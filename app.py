from flask import Flask, request, abort
import requests
import hashlib
import hmac
import base64
import os
import re
import random
import time
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

app = Flask(__name__)


# =========================================================
# 환경변수
# =========================================================

CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET")

SUPABASE_URL = os.environ.get(
    "SUPABASE_URL",
    ""
).rstrip("/")

SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


# =========================================================
# HTTP Session
# 연결을 매번 새로 만들지 않고 재사용
# =========================================================

HTTP = requests.Session()

HTTP.headers.update({
    "User-Agent": "LINE-Bot/1.0"
})


# =========================================================
# 기본 설정
# =========================================================

KST = ZoneInfo("Asia/Seoul")

AI_TRIGGER_CHANCE = 0.03

AI_COOLDOWN_SECONDS = 1 * 60 * 60

RECENT_MESSAGE_LIMIT = 12


# =========================================================
# Gemini 모델
# =========================================================

NORMAL_GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
]

SEARCH_GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
]

NORMAL_AI_TIMEOUT = 8
SEARCH_AI_TIMEOUT = 15


# =========================================================
# 캐시
# =========================================================

PROFILE_CACHE = {}
PROFILE_CACHE_SECONDS = 600


GROUP_MEMBER_CACHE = {}
GROUP_MEMBER_CACHE_SECONDS = 60


# =========================================================
# 시간
# =========================================================

def now_kst():
    return datetime.now(KST)


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


# =========================================================
# LINE 서명
# =========================================================

def verify_signature(body, signature):

    if not CHANNEL_SECRET:
        return False

    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()

    expected = base64.b64encode(
        digest
    ).decode("utf-8")

    return hmac.compare_digest(
        expected,
        signature or ""
    )


# =========================================================
# LINE API
# =========================================================

def line_api(endpoint, payload):

    url = (
        "https://api.line.me/v2/bot/"
        + endpoint
    )

    headers = {
        "Authorization":
            f"Bearer {CHANNEL_ACCESS_TOKEN}",

        "Content-Type":
            "application/json"
    }

    try:

        response = HTTP.post(
            url,
            headers=headers,
            json=payload,
            timeout=8
        )

        if response.status_code >= 400:

            print(
                "LINE API ERROR:",
                endpoint,
                response.status_code,
                response.text[:500]
            )

        return response

    except Exception as e:

        print(
            "LINE API EXCEPTION:",
            repr(e)
        )

        return None


# =========================================================
# LINE GET
# =========================================================

def line_get(endpoint, params=None):

    url = (
        "https://api.line.me/v2/bot/"
        + endpoint
    )

    headers = {
        "Authorization":
            f"Bearer {CHANNEL_ACCESS_TOKEN}"
    }

    try:

        response = HTTP.get(
            url,
            headers=headers,
            params=params or {},
            timeout=6
        )

        if response.status_code >= 400:

            print(
                "LINE GET ERROR:",
                endpoint,
                response.status_code
            )

        return response

    except Exception as e:

        print(
            "LINE GET EXCEPTION:",
            repr(e)
        )

        return None


# =========================================================
# Reply API
# =========================================================

def reply_message(reply_token, text):

    if not reply_token:
        return

    text = str(text)

    if len(text) > 4900:
        text = text[:4900]

    line_api(
        "message/reply",
        {
            "replyToken": reply_token,
            "messages": [
                {
                    "type": "text",
                    "text": text
                }
            ]
        }
    )


# =========================================================
# Push API
# =========================================================

def push_message(group_id, text):

    if not group_id:
        return

    text = str(text)

    if len(text) > 4900:
        text = text[:4900]

    line_api(
        "message/push",
        {
            "to": group_id,
            "messages": [
                {
                    "type": "text",
                    "text": text
                }
            ]
        }
    )


# =========================================================
# 프로필
# =========================================================

def get_profile_name(user_id, group_id=None):

    if not user_id:
        return "알 수 없음"

    cache_key = (
        group_id,
        user_id
    )

    cached = PROFILE_CACHE.get(cache_key)

    if cached:

        if (
            time.time()
            - cached["time"]
            < PROFILE_CACHE_SECONDS
        ):
            return cached["name"]

    headers = {
        "Authorization":
            f"Bearer {CHANNEL_ACCESS_TOKEN}"
    }

    try:

        if group_id:

            url = (
                "https://api.line.me/v2/bot/group/"
                f"{group_id}/member/{user_id}"
            )

        else:

            url = (
                "https://api.line.me/v2/bot/profile/"
                f"{user_id}"
            )

        response = HTTP.get(
            url,
            headers=headers,
            timeout=4
        )

        if response.status_code == 200:

            data = response.json()

            name = data.get(
                "displayName",
                "알 수 없음"
            )

            PROFILE_CACHE[cache_key] = {
                "name": name,
                "time": time.time()
            }

            return name

    except Exception as e:

        print(
            "PROFILE ERROR:",
            repr(e)
        )

    return "알 수 없음"


# =========================================================
# 그룹 멤버 ID
# =========================================================

def get_group_member_ids(group_id):

    if not group_id:
        return []

    member_ids = []
    start = None

    while True:

        params = {}

        if start:
            params["start"] = start

        response = line_get(
            f"group/{group_id}/members/ids",
            params
        )

        if (
            response is None
            or response.status_code != 200
        ):
            break

        try:
            data = response.json()
        except Exception:
            break

        member_ids.extend(
            data.get(
                "memberIds",
                []
            )
        )

        if not data.get(
            "hasNext",
            False
        ):
            break

        start = data.get("next")

        if not start:
            break

    return member_ids


# =========================================================
# 그룹 멤버
# 1분 캐시
# =========================================================

def get_group_members(group_id):

    if not group_id:
        return {}

    cached = GROUP_MEMBER_CACHE.get(
        group_id
    )

    if cached:

        if (
            time.time()
            - cached["time"]
            < GROUP_MEMBER_CACHE_SECONDS
        ):

            return cached["members"].copy()

    member_ids = get_group_member_ids(
        group_id
    )

    members = {}

    for user_id in member_ids:

        members[user_id] = get_profile_name(
            user_id,
            group_id
        )

    GROUP_MEMBER_CACHE[group_id] = {
        "members": members,
        "time": time.time()
    }

    return members.copy()


# =========================================================
# Supabase
# =========================================================

def supabase_headers():

    return {
        "apikey": SUPABASE_KEY,
        "Authorization":
            f"Bearer {SUPABASE_KEY}",
        "Content-Type":
            "application/json"
    }


def supabase_get(table, params=None):

    if (
        not SUPABASE_URL
        or not SUPABASE_KEY
    ):
        return []

    try:

        response = HTTP.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=supabase_headers(),
            params=params or {},
            timeout=5
        )

        if response.status_code >= 400:

            print(
                "SUPABASE GET ERROR:",
                response.status_code
            )

            return []

        return response.json()

    except Exception as e:

        print(
            "SUPABASE GET EXCEPTION:",
            repr(e)
        )

        return []


# =========================================================
# Supabase 전체 조회
# =========================================================

SUPABASE_PAGE_SIZE = 1000


def supabase_get_all(table, params=None):

    if (
        not SUPABASE_URL
        or not SUPABASE_KEY
    ):
        return []

    base_params = dict(
        params or {}
    )

    base_params.pop("limit", None)
    base_params.pop("offset", None)

    all_rows = []
    offset = 0

    while True:

        page_params = dict(
            base_params
        )

        page_params["limit"] = str(
            SUPABASE_PAGE_SIZE
        )

        page_params["offset"] = str(
            offset
        )

        try:

            response = HTTP.get(
                f"{SUPABASE_URL}/rest/v1/{table}",
                headers=supabase_headers(),
                params=page_params,
                timeout=8
            )

            if response.status_code >= 400:

                print(
                    "SUPABASE PAGINATION ERROR:",
                    response.status_code
                )

                break

            rows = response.json()

            if not isinstance(
                rows,
                list
            ):
                break

            all_rows.extend(rows)

            if len(rows) < SUPABASE_PAGE_SIZE:
                break

            offset += SUPABASE_PAGE_SIZE

        except Exception as e:

            print(
                "SUPABASE PAGINATION EXCEPTION:",
                repr(e)
            )

            break

    return all_rows


# =========================================================
# Supabase POST
# =========================================================

def supabase_post(table, data):

    if (
        not SUPABASE_URL
        or not SUPABASE_KEY
    ):
        return None

    headers = supabase_headers()

    headers["Prefer"] = "return=minimal"

    try:

        return HTTP.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            json=data,
            timeout=5
        )

    except Exception as e:

        print(
            "SUPABASE POST ERROR:",
            repr(e)
        )

        return None


# =========================================================
# Supabase PATCH
# =========================================================

def supabase_patch(table, params, data):

    if (
        not SUPABASE_URL
        or not SUPABASE_KEY
    ):
        return None

    headers = supabase_headers()

    headers["Prefer"] = "return=minimal"

    try:

        return HTTP.patch(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            params=params,
            json=data,
            timeout=5
        )

    except Exception as e:

        print(
            "SUPABASE PATCH ERROR:",
            repr(e)
        )

        return None


# =========================================================
# Supabase DELETE
# =========================================================

def supabase_delete(table, params):

    if (
        not SUPABASE_URL
        or not SUPABASE_KEY
    ):
        return None

    try:

        return HTTP.delete(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=supabase_headers(),
            params=params,
            timeout=5
        )

    except Exception as e:

        print(
            "SUPABASE DELETE ERROR:",
            repr(e)
        )

        return None


# =========================================================
# 이벤트 시간
# =========================================================

def get_event_datetime(event):

    timestamp = event.get(
        "timestamp"
    )

    if timestamp is None:
        return now_kst()

    try:

        dt = datetime.fromtimestamp(
            int(timestamp) / 1000,
            tz=timezone.utc
        )

        return dt.astimezone(KST)

    except Exception:

        return now_kst()


# =========================================================
# 소통량 저장
# =========================================================

def save_chat_message(
    group_id,
    user_id,
    user_name,
    message,
    event_time=None
):

    if not group_id:
        return False

    if event_time is None:
        event_time = now_kst()

    data = {
        "group_id": group_id,
        "user_id": user_id,
        "user_name": user_name,
        "message": message,
        "date_kst":
            event_time.strftime("%Y-%m-%d"),
        "hour_kst":
            event_time.hour
    }

    response = supabase_post(
        "chat_messages",
        data
    )

    if (
        response is not None
        and response.status_code < 300
    ):

        return True

    return False


def is_laughter_only(text):

    if not text:
        return False

    return bool(
        re.fullmatch(
            r"[ㅋㅎㅠㅜ]+",
            text.strip()
        )
    )


# =========================================================
# 오늘 메시지
# 필요한 데이터만 가져옴
# =========================================================

def get_today_messages(group_id):

    today = now_kst().strftime(
        "%Y-%m-%d"
    )

    return supabase_get_all(
        "chat_messages",
        {
            "group_id":
                f"eq.{group_id}",

            "date_kst":
                f"eq.{today}",

            "select":
                "user_id,user_name,date_kst,hour_kst",

            "order":
                "created_at.asc"
        }
    )


# =========================================================
# 전체 메시지
# 통계에 필요한 데이터만 가져옴
# =========================================================

def get_all_group_messages(group_id):

    return supabase_get_all(
        "chat_messages",
        {
            "group_id":
                f"eq.{group_id}",

            "select":
                "user_id,user_name,date_kst,hour_kst",

            "order":
                "created_at.asc"
        }
    )


# =========================================================
# 오늘 소통량
# =========================================================

def today_ranking(group_id):

    rows = get_today_messages(
        group_id
    )

    counts = {}
    names = {}

    current_members = get_group_members(
        group_id
    )

    for user_id, name in current_members.items():

        counts[user_id] = 0
        names[user_id] = name

    for row in rows:

        user_id = row.get(
            "user_id"
        )

        if not user_id:
            continue

        name = row.get(
            "user_name",
            "알 수 없음"
        )

        if user_id not in names:
            names[user_id] = name

        if user_id not in counts:
            counts[user_id] = 0

        counts[user_id] += 1

    ranking = sorted(
        counts.items(),
        key=lambda x: (
            -x[1],
            names.get(
                x[0],
                ""
            )
        )
    )

    return ranking, names


def make_ranking_text(group_id):

    ranking, names = today_ranking(
        group_id
    )

    if not ranking:

        return (
            "현재 그룹 멤버를 가져오지 못했어."
        )

    result = (
        "🏆 오늘 단라 소통량 순위\n\n"
    )

    for index, (
        user_id,
        count
    ) in enumerate(
        ranking,
        start=1
    ):

        name = names.get(
            user_id,
            "알 수 없음"
        )

        line = (
            f"{index}위 {name} — "
            f"{count}개\n"
        )

        if len(result) + len(line) > 4700:

            result += (
                "\n※ 인원이 많아 나머지는 생략했어."
            )

            break

        result += line

    return result.strip()


# =========================================================
# 내 소통량
# =========================================================

def make_my_count_text(
    group_id,
    user_id,
    user_name
):

    rows = get_today_messages(
        group_id
    )

    count = sum(
        1
        for row in rows
        if row.get("user_id") == user_id
    )

    return (
        f"💬 {user_name}님의 오늘 소통량\n"
        f"{count}개"
    )


# =========================================================
# 방 통계
# =========================================================

def make_room_stats(group_id):

    rows = get_all_group_messages(
        group_id
    )

    if not rows:
        return "아직 통계가 없어!"

    total = len(rows)

    users = set()
    day_counts = {}

    for row in rows:

        user_id = row.get(
            "user_id"
        )

        if user_id:
            users.add(user_id)

        date = row.get(
            "date_kst"
        )

        if date:

            day_counts[date] = (
                day_counts.get(
                    date,
                    0
                ) + 1
            )

    active_day = None

    if day_counts:

        active_day = max(
            day_counts.items(),
            key=lambda x: x[1]
        )

    result = (
        "📊 방 통계\n\n"
        f"총 메시지: {total}개\n"
        f"참여 인원: {len(users)}명\n"
    )

    if active_day:

        result += (
            f"가장 활발했던 날: "
            f"{active_day[0]} "
            f"({active_day[1]}개)"
        )

    return result


# =========================================================
# 내 통계
# =========================================================

def make_my_stats(
    group_id,
    user_id,
    user_name
):

    rows = get_all_group_messages(
        group_id
    )

    if not rows:
        return "아직 통계가 없어!"

    counts = {}
    hour_counts = {}

    for row in rows:

        uid = row.get(
            "user_id"
        )

        if uid:

            counts[uid] = (
                counts.get(
                    uid,
                    0
                ) + 1
            )

        if uid == user_id:

            hour = row.get(
                "hour_kst"
            )

            if hour is not None:

                hour_counts[hour] = (
                    hour_counts.get(
                        hour,
                        0
                    ) + 1
                )

    my_count = counts.get(
        user_id,
        0
    )

    ranking = sorted(
        counts.items(),
        key=lambda x: x[1],
        reverse=True
    )

    rank = 0

    for index, item in enumerate(
        ranking,
        start=1
    ):

        if item[0] == user_id:

            rank = index
            break

    active_hour = None

    if hour_counts:

        active_hour = max(
            hour_counts.items(),
            key=lambda x: x[1]
        )

    result = (
        f"📊 {user_name}님의 통계\n\n"
        f"총 소통량: {my_count}개\n"
        f"방 내 순위: {rank}위"
    )

    if active_hour:

        result += (
            f"\n가장 활발한 시간: "
            f"{active_hour[0]}시 "
            f"({active_hour[1]}개)"
        )

    return result


# =========================================================
# 소통량 초기화
# =========================================================

def reset_today(group_id):

    today = now_kst().strftime(
        "%Y-%m-%d"
    )

    response = supabase_delete(
        "chat_messages",
        {
            "group_id":
                f"eq.{group_id}",

            "date_kst":
                f"eq.{today}"
        }
    )

    return (
        response is not None
        and response.status_code < 300
    )


# =========================================================
# 닉네임 변환
# =========================================================

SUPERSCRIPT_MAP = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹"
}


def superscript_age(age):

    age = str(age).zfill(2)

    return "".join(
        SUPERSCRIPT_MAP.get(
            char,
            char
        )
        for char in age
    )


def make_name_format(text):

    parts = text.strip().split()

    if len(parts) != 4:
        return None

    name, gender, age, kind = parts

    if not re.fullmatch(
        r"\d{1,2}",
        age
    ):
        return None

    gender = gender.lower()
    kind = kind.lower()

    gender_map = {
        "여자": "📡",
        "여": "📡",
        "남자": "🔭",
        "남": "🔭"
    }

    kind_map = {
        "돔": "𝒅",
        "섭": "𝒔",
        "스위치": "𝒔/𝒅",
        "바닐라": "𝒗"
    }

    if gender not in gender_map:
        return None

    if kind not in kind_map:
        return None

    return (
        f"{name}"
        f"{kind_map[kind]}"
        f"{superscript_age(age)}"
        f"{gender_map[gender]}"
    )


# =========================================================
# 최근 대화
# =========================================================

def get_recent_messages(group_id):

    rows = supabase_get(
        "chat_messages",
        {
            "group_id":
                f"eq.{group_id}",

            "select":
                "user_name,message,created_at",

            "order":
                "created_at.desc",

            "limit":
                str(RECENT_MESSAGE_LIMIT)
        }
    )

    rows.reverse()

    return "\n".join(
        f"{row.get('user_name', '알 수 없음')}: "
        f"{row.get('message', '')}"
        for row in rows
    )


# =========================================================
# Gemini
# =========================================================

def gemini_request(
    model,
    prompt,
    use_search=False,
    timeout=8
):

    if not GEMINI_API_KEY:
        return None

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    body = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.8,
            "maxOutputTokens": 300
        }
    }

    if use_search:

        body["tools"] = [
            {
                "google_search": {}
            }
        ]

    started = time.time()

    try:

        response = HTTP.post(
            url,
            params={
                "key": GEMINI_API_KEY
            },
            json=body,
            timeout=timeout
        )

        elapsed = time.time() - started

        print(
            f"GEMINI {model}: "
            f"{response.status_code} "
            f"{elapsed:.2f}s"
        )

        if response.status_code != 200:
            return None

        data = response.json()

        candidates = data.get(
            "candidates",
            []
        )

        if not candidates:
            return None

        parts = (
            candidates[0]
            .get("content", {})
            .get("parts", [])
        )

        answer = "".join(
            part["text"]
            for part in parts
            if "text" in part
        ).strip()

        return answer or None

    except Exception as e:

        print(
            "GEMINI ERROR:",
            repr(e)
        )

        return None


# =========================================================
# 실시간 검색 키워드
# =========================================================

REALTIME_KEYWORDS = [
    "지금",
    "현재",
    "오늘",
    "오늘 날짜",
    "오늘날짜",
    "내일",
    "어제",
    "이번주",
    "이번 주",
    "이번달",
    "이번 달",
    "최신",
    "최근",
    "요즘",
    "업데이트",
    "뉴스",
    "속보",
    "사건",
    "사고",
    "날씨",
    "기온",
    "미세먼지",
    "비 와",
    "비와",
    "눈 와",
    "눈와",
    "경기 결과",
    "경기결과",
    "경기 일정",
    "경기일정",
    "스코어",
    "현재 순위",
    "환율",
    "주가",
    "주식",
    "코인",
    "비트코인",
    "가격",
    "시세",
    "일정",
    "예매",
    "예약",
    "검색해",
    "검색해서",
    "찾아줘",
    "찾아봐",
    "인터넷에서",
    "웹에서"
]


def needs_realtime_search(prompt):

    text = prompt.lower()

    return any(
        keyword.lower() in text
        for keyword in REALTIME_KEYWORDS
    )


# =========================================================
# 일반 AI
# =========================================================

def ask_normal_gemini(prompt):

    system = """
너는 한국 단체 채팅방의 자연스러운 AI 친구다.

사용자의 질문에 정확하고 자연스럽게 답한다.

규칙:
- 한국어로 답한다.
- 너무 길게 답하지 않는다.
- 친근하고 자연스럽게 말한다.
- 질문에 바로 답한다.
- 모르면 모른다고 말한다.
- 없는 사실을 만들어내지 않는다.
- 정치, 종교, 성적인 내용, 개인정보,
  싸움이나 심각한 문제에는 불필요하게 끼어들지 않는다.
- 일반 질문에서는 인터넷 검색을 하지 않는다.
"""

    full_prompt = (
        system
        + "\n\n사용자 질문:\n"
        + prompt
    )

    for model in NORMAL_GEMINI_MODELS:

        answer = gemini_request(
            model,
            full_prompt,
            use_search=False,
            timeout=NORMAL_AI_TIMEOUT
        )

        if answer:
            return answer

    return (
        "지금 AI가 잠깐 바빠서 "
        "답변을 못 했어 😭"
    )


# =========================================================
# 검색 AI
# =========================================================

def ask_search_gemini(prompt):

    system = """
너는 한국 단체 채팅방의 AI 친구다.

사용자의 질문에 답하기 위해 최신 정보가 필요하면
Google 검색을 사용한다.

규칙:
- 한국어로 답한다.
- 검색 결과를 바탕으로 정확하게 답한다.
- 최신 정보가 필요한 질문은 검색한다.
- 너무 길게 답하지 않는다.
- 모르는 것은 모른다고 한다.
- 검색 결과에 없는 내용을 지어내지 않는다.
"""

    full_prompt = (
        system
        + "\n\n사용자 질문:\n"
        + prompt
    )

    for model in SEARCH_GEMINI_MODELS:

        answer = gemini_request(
            model,
            full_prompt,
            use_search=True,
            timeout=SEARCH_AI_TIMEOUT
        )

        if answer:
            return answer

    return (
        "지금 검색 AI가 잠깐 바빠서 "
        "답변을 못 했어 😭"
    )


# =========================================================
# AI 질문
# =========================================================

def answer_ai_question(prompt):

    realtime = needs_realtime_search(
        prompt
    )

    print(
        f"AI QUESTION: {prompt} | "
        f"realtime={realtime}"
    )

    if realtime:

        return ask_search_gemini(
            prompt
        )

    return ask_normal_gemini(
        prompt
    )


# =========================================================
# 랜덤 AI 상태
# =========================================================

def get_last_ai_reply(group_id):

    rows = supabase_get(
        "bot_state",
        {
            "group_id":
                f"eq.{group_id}",

            "select":
                "last_ai_reply",

            "limit":
                "1"
        }
    )

    if not rows:
        return None

    value = rows[0].get(
        "last_ai_reply"
    )

    if not value:
        return None

    try:

        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        )

    except Exception:

        return None


def can_random_ai_reply(group_id):

    last = get_last_ai_reply(
        group_id
    )

    if not last:
        return True

    elapsed = (
        datetime.now(
            timezone.utc
        ) - last
    ).total_seconds()

    return (
        elapsed
        >= AI_COOLDOWN_SECONDS
    )


def save_last_ai_reply(group_id):

    existing = supabase_get(
        "bot_state",
        {
            "group_id":
                f"eq.{group_id}",

            "select":
                "group_id",

            "limit":
                "1"
        }
    )

    data = {
        "group_id":
            group_id,

        "last_ai_reply":
            now_utc_iso()
    }

    if existing:

        supabase_patch(
            "bot_state",
            {
                "group_id":
                    f"eq.{group_id}"
            },
            data
        )

    else:

        supabase_post(
            "bot_state",
            data
        )


# =========================================================
# 랜덤 AI
# =========================================================

def ask_random_ai(
    group_id,
    reply_token
):

    recent = get_recent_messages(
        group_id
    )

    if not recent:
        return

    prompt = f"""
너는 단체 채팅방에 아주 가끔 자연스럽게 끼어드는 AI 친구다.

최근 대화:
{recent}

지금 대화 흐름을 보고 판단해라.

정말 자연스럽게 한마디 끼어들 이유가 있을 때만:

YES|짧은 답변

아무 이유가 없으면:

NO

규칙:
- 억지로 끼어들지 않는다.
- 대화 흐름과 관련 있을 때만 말한다.
- 짧고 자연스럽게 말한다.
- 친구처럼 말한다.
- 정치, 종교, 성적인 내용, 개인정보,
  싸움이나 심각한 문제에는 끼어들지 않는다.
"""

    for model in NORMAL_GEMINI_MODELS:

        answer = gemini_request(
            model,
            prompt,
            use_search=False,
            timeout=NORMAL_AI_TIMEOUT
        )

        if not answer:
            continue

        answer = answer.strip()

        if answer.upper() == "NO":
            return

        if answer.upper().startswith("YES|"):

            reply = answer[4:].strip()

            if reply:

                save_last_ai_reply(
                    group_id
                )

                # 기존과 동일하게 Reply API 사용
                reply_message(
                    reply_token,
                    reply
                )

            return


def maybe_start_random_ai(
    group_id,
    reply_token
):

    if not group_id:
        return

    if not can_random_ai_reply(
        group_id
    ):
        return

    if random.random() > AI_TRIGGER_CHANCE:
        return

    thread = threading.Thread(
        target=ask_random_ai,
        args=(
            group_id,
            reply_token
        ),
        daemon=True
    )

    thread.start()


# =========================================================
# 블랙잭
# =========================================================

BLACKJACK_SUITS = [
    "♠️",
    "♥️",
    "♦️",
    "♣️"
]

BLACKJACK_RANKS = [
    "A",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "10",
    "J",
    "Q",
    "K"
]


def blackjack_new_deck():

    deck = [
        (rank, suit)
        for suit in BLACKJACK_SUITS
        for rank in BLACKJACK_RANKS
    ]

    random.shuffle(deck)

    return deck


def blackjack_card_value(rank):

    if rank == "A":
        return 11

    if rank in [
        "J",
        "Q",
        "K"
    ]:
        return 10

    return int(rank)


def blackjack_score(cards):

    if not cards:
        return 0

    score = sum(
        blackjack_card_value(rank)
        for rank, _ in cards
    )

    aces = sum(
        1
        for rank, _ in cards
        if rank == "A"
    )

    while score > 21 and aces:

        score -= 10
        aces -= 1

    return score


def blackjack_score_text(cards):

    if not cards:
        return "카드 없음"

    score = blackjack_score(
        cards
    )

    raw_score = sum(
        blackjack_card_value(rank)
        for rank, _ in cards
    )

    aces = sum(
        1
        for rank, _ in cards
        if rank == "A"
    )

    if (
        aces > 0
        and raw_score > 21
        and score != raw_score
    ):

        return f"{score} (A=1 적용)"

    return str(score)


def blackjack_cards_text(cards):

    if not cards:
        return "카드 없음"

    return "  ".join(
        f"{rank}{suit}"
        for rank, suit in cards
    )


# =========================================================
# 1인 블랙잭
# =========================================================

BLACKJACK_GAMES = {}


def blackjack_dealer_text(
    cards,
    hide_second=False
):

    if (
        hide_second
        and len(cards) >= 2
    ):

        visible = (
            blackjack_cards_text(
                cards[:1]
            )
            + "  🂠"
        )

        return (
            "🤖 딜러\n"
            f"🃏 {visible}\n"
            "합계: ?"
        )

    return (
        "🤖 딜러\n"
        f"🃏 {blackjack_cards_text(cards)}\n"
        f"합계: {blackjack_score_text(cards)}"
    )


def blackjack_finish_text(
    player_name,
    game,
    reason=""
):

    player_score = blackjack_score(
        game["player_cards"]
    )

    dealer_score = blackjack_score(
        game["dealer_cards"]
    )

    if player_score > 21:

        result = (
            "💥 버스트! "
            "네가 21을 넘어서 패배했어."
        )

    elif dealer_score > 21:

        result = (
            "🎉 딜러가 버스트했어! "
            "네 승리!"
        )

    elif player_score > dealer_score:

        result = "🎉 네 승리!"

    elif player_score < dealer_score:

        result = "😢 딜러 승리!"

    else:

        result = "🤝 무승부!"

    prefix = (
        f"{reason}\n"
        if reason
        else ""
    )

    return (
        f"{prefix}"
        "🎰 블랙잭 결과\n\n"

        f"👤 {player_name}\n"
        f"🃏 {blackjack_cards_text(game['player_cards'])}\n"
        f"합계: "
        f"{blackjack_score_text(game['player_cards'])}\n\n"

        f"{blackjack_dealer_text(game['dealer_cards'])}\n\n"

        f"{result}\n\n"
        "새 게임은 `블랙잭`을 입력해!"
    )


def blackjack_start(
    group_id,
    user_id,
    user_name
):

    key = (
        group_id,
        user_id
    )

    if key in BLACKJACK_GAMES:

        return (
            "이미 블랙잭을 하고 있어!\n"
            "`히트` 또는 `스탠드`를 입력해.\n"
            "그만하려면 "
            "`블랙잭 종료`를 입력해."
        )

    deck = blackjack_new_deck()

    game = {
        "deck": deck,

        "player_cards": [
            deck.pop(),
            deck.pop()
        ],

        "dealer_cards": [
            deck.pop(),
            deck.pop()
        ],

        "user_name":
            user_name
    }

    BLACKJACK_GAMES[key] = game

    player_score = blackjack_score(
        game["player_cards"]
    )

    dealer_score = blackjack_score(
        game["dealer_cards"]
    )

    if (
        player_score == 21
        or dealer_score == 21
    ):

        del BLACKJACK_GAMES[key]

        if (
            player_score == 21
            and dealer_score == 21
        ):

            return blackjack_finish_text(
                user_name,
                game,
                "처음 받은 카드로 둘 다 블랙잭!"
            )

        if player_score == 21:

            return blackjack_finish_text(
                user_name,
                game,
                "🃏 블랙잭! 처음 받은 카드로 21을 만들었어!"
            )

        return blackjack_finish_text(
            user_name,
            game,
            "딜러가 처음부터 블랙잭이야!"
        )

    return (
        "🎰 블랙잭 시작!\n\n"

        f"👤 {user_name}\n"
        f"🃏 {blackjack_cards_text(game['player_cards'])}\n"
        f"합계: "
        f"{blackjack_score_text(game['player_cards'])}\n\n"

        f"{blackjack_dealer_text(game['dealer_cards'], True)}\n\n"

        "`히트` — 카드 한 장 더 받기\n"
        "`스탠드` — 카드 받기 멈추기\n"
        "`블랙잭 종료` — 게임 종료"
    )


def blackjack_hit(
    group_id,
    user_id,
    user_name
):

    key = (
        group_id,
        user_id
    )

    game = BLACKJACK_GAMES.get(
        key
    )

    if not game:

        return (
            "진행 중인 블랙잭이 없어! "
            "먼저 `블랙잭`을 입력해."
        )

    if not game["deck"]:

        game["deck"] = blackjack_new_deck()

    game["player_cards"].append(
        game["deck"].pop()
    )

    score = blackjack_score(
        game["player_cards"]
    )

    if score >= 21:

        if score > 21:

            del BLACKJACK_GAMES[key]

            return blackjack_finish_text(
                user_name,
                game
            )

        while (
            blackjack_score(
                game["dealer_cards"]
            ) < 17
            and game["deck"]
        ):

            game["dealer_cards"].append(
                game["deck"].pop()
            )

        del BLACKJACK_GAMES[key]

        return blackjack_finish_text(
            user_name,
            game,
            "21을 만들었어! 딜러 차례야."
        )

    return (
        f"👤 {user_name}\n"
        f"🃏 {blackjack_cards_text(game['player_cards'])}\n"
        f"합계: "
        f"{blackjack_score_text(game['player_cards'])}\n\n"

        f"{blackjack_dealer_text(game['dealer_cards'], True)}\n\n"

        "`히트` 또는 `스탠드`를 입력해!"
    )


def blackjack_stand(
    group_id,
    user_id,
    user_name
):

    key = (
        group_id,
        user_id
    )

    game = BLACKJACK_GAMES.get(
        key
    )

    if not game:

        return (
            "진행 중인 블랙잭이 없어! "
            "먼저 `블랙잭`을 입력해."
        )

    while (
        blackjack_score(
            game["dealer_cards"]
        ) < 17
        and game["deck"]
    ):

        game["dealer_cards"].append(
            game["deck"].pop()
        )

    del BLACKJACK_GAMES[key]

    return blackjack_finish_text(
        user_name,
        game
    )


def blackjack_cancel(
    group_id,
    user_id
):

    key = (
        group_id,
        user_id
    )

    if BLACKJACK_GAMES.pop(
        key,
        None
    ) is None:

        return (
            "종료할 블랙잭 게임이 없어."
        )

    return (
        "🛑 블랙잭을 종료했어.\n"
        "다시 하려면 `블랙잭`을 입력해!"
    )


# =========================================================
# 유저 vs 유저 블랙잭
# =========================================================

BLACKJACK_DUELS = {}
BLACKJACK_CHALLENGES = {}


def blackjack_duel_board(
    duel,
    reveal_all=False
):

    lines = [
        "🎰 블랙잭 대결 진행 중!\n"
    ]

    for uid in duel["players"]:

        name = duel["names"][uid]
        cards = duel["cards"][uid]

        status = (
            " (스탠드)"
            if uid in duel["stood"]
            else ""
        )

        lines.append(
            f"👤 {name}{status}\n"
            f"🃏 {blackjack_cards_text(cards)}\n"
            f"합계: "
            f"{blackjack_score_text(cards)}"
        )

    if not reveal_all:

        current = duel[
            "players"
        ][
            duel["turn"]
        ]

        lines.append(
            f"\n➡️ {duel['names'][current]}의 차례!\n"
            "`히트` 또는 `스탠드`"
        )

    return "\n\n".join(lines)


def blackjack_duel_result(
    duel,
    reason=""
):

    lines = [
        "🎰 블랙잭 대결 결과!\n"
    ]

    scores = {}

    for uid in duel["players"]:

        cards = duel["cards"][uid]

        score = blackjack_score(
            cards
        )

        scores[uid] = score

        lines.append(
            f"👤 {duel['names'][uid]}\n"
            f"🃏 {blackjack_cards_text(cards)}\n"
            f"합계: "
            f"{blackjack_score_text(cards)}"
        )

    a, b = duel["players"]

    sa = scores[a]
    sb = scores[b]

    if sa > 21 and sb > 21:

        result = (
            "🤝 둘 다 버스트! 무승부!"
        )

    elif sa > 21:

        result = (
            f"🏆 {duel['names'][b]} 승리! "
            f"({sa} vs {sb})"
        )

    elif sb > 21:

        result = (
            f"🏆 {duel['names'][a]} 승리! "
            f"({sa} vs {sb})"
        )

    elif sa > sb:

        result = (
            f"🏆 {duel['names'][a]} 승리! "
            f"({sa} vs {sb})"
        )

    elif sb > sa:

        result = (
            f"🏆 {duel['names'][b]} 승리! "
            f"({sb} vs {sa})"
        )

    else:

        result = (
            f"🤝 무승부! "
            f"({sa} vs {sb})"
        )

    prefix = (
        f"{reason}\n\n"
        if reason
        else ""
    )

    return (
        prefix
        + "\n\n".join(lines)
        + "\n\n"
        + result
        + "\n\n"
        + "새 대결은 "
        "`블랙잭 상대이름`으로 신청해!"
    )


def blackjack_duel_advance(duel):

    if len(duel["stood"]) == 2:

        return blackjack_duel_result(
            duel
        )

    for offset in (1, 2):

        index = (
            duel["turn"]
            + offset
        ) % 2

        uid = duel["players"][index]

        if uid not in duel["stood"]:

            duel["turn"] = index

            return blackjack_duel_board(
                duel
            )

    return blackjack_duel_result(
        duel
    )


def blackjack_duel_challenge(
    group_id,
    challenger_id,
    challenger_name,
    target_name
):

    if group_id in BLACKJACK_DUELS:

        return (
            "이 방에서는 이미 "
            "블랙잭 대결이 진행 중이야!"
        )

    if group_id in BLACKJACK_CHALLENGES:

        return (
            "이미 수락을 기다리는 "
            "대결 신청이 있어."
        )

    target_name = target_name.strip()

    if not target_name:

        return (
            "상대 이름을 입력해 줘.\n"
            "예: `블랙잭 철수`"
        )

    if target_name == challenger_name:

        return (
            "자기 자신에게는 "
            "대결을 신청할 수 없어!"
        )

    BLACKJACK_CHALLENGES[group_id] = {
        "challenger_id":
            challenger_id,

        "challenger_name":
            challenger_name,

        "target_name":
            target_name,

        "created_at":
            time.time()
    }

    return (
        "🃏 블랙잭 대결 신청!\n\n"

        f"{challenger_name} ⚔️ "
        f"{target_name}\n\n"

        f"{target_name}님, "
        "수락하려면 `수락`, "
        "거절하려면 `거절`을 입력해!"
    )


def blackjack_duel_accept(
    group_id,
    user_id,
    user_name
):

    challenge = BLACKJACK_CHALLENGES.get(
        group_id
    )

    if not challenge:

        return (
            "수락할 블랙잭 "
            "대결 신청이 없어."
        )

    if (
        time.time()
        - challenge["created_at"]
        > 600
    ):

        BLACKJACK_CHALLENGES.pop(
            group_id,
            None
        )

        return (
            "대결 신청이 만료됐어. "
            "다시 신청해 줘!"
        )

    if (
        user_id
        == challenge["challenger_id"]
    ):

        return (
            "신청한 사람은 "
            "상대방이 수락해야 해!"
        )

    if user_name != challenge["target_name"]:

        return (
            f"{challenge['target_name']}님만 "
            "수락할 수 있어!"
        )

    BLACKJACK_CHALLENGES.pop(
        group_id,
        None
    )

    deck = blackjack_new_deck()

    a = challenge["challenger_id"]
    b = user_id

    duel = {
        "players": [a, b],

        "names": {
            a:
                challenge["challenger_name"],

            b:
                user_name
        },

        "cards": {
            a: [
                deck.pop(),
                deck.pop()
            ],

            b: [
                deck.pop(),
                deck.pop()
            ]
        },

        "deck": deck,

        "stood": set(),

        "turn": 0
    }

    BLACKJACK_DUELS[group_id] = duel

    for uid in duel["players"]:

        if (
            blackjack_score(
                duel["cards"][uid]
            )
            == 21
        ):

            duel["stood"].add(uid)

    if len(duel["stood"]) == 2:

        result = blackjack_duel_result(
            duel,
            "처음 받은 카드로 둘 다 21이야!"
        )

        BLACKJACK_DUELS.pop(
            group_id,
            None
        )

        return result

    if (
        duel["players"][0]
        in duel["stood"]
    ):

        duel["turn"] = 1

    return (
        "🎰 블랙잭 대결 시작!\n\n"
        + blackjack_duel_board(
            duel
        )
    )


def blackjack_duel_decline(
    group_id,
    user_id,
    user_name
):

    challenge = BLACKJACK_CHALLENGES.get(
        group_id
    )

    if not challenge:

        return (
            "거절할 블랙잭 "
            "대결 신청이 없어."
        )

    if (
        user_id
        == challenge["challenger_id"]
        or
        user_name
        != challenge["target_name"]
    ):

        return (
            f"{challenge['target_name']}님만 "
            "거절할 수 있어."
        )

    BLACKJACK_CHALLENGES.pop(
        group_id,
        None
    )

    return (
        f"❌ {user_name}님이 "
        f"{challenge['challenger_name']}님의 "
        "블랙잭 대결을 거절했어."
    )


def blackjack_duel_action(
    group_id,
    user_id,
    action
):

    duel = BLACKJACK_DUELS.get(
        group_id
    )

    if not duel:
        return None

    if user_id not in duel["players"]:

        return (
            "현재 대결 중인 "
            "두 사람만 히트 또는 "
            "스탠드를 할 수 있어."
        )

    if user_id in duel["stood"]:

        return (
            "너는 이미 스탠드했어. "
            "상대방 차례를 기다려 줘."
        )

    current = duel["players"][
        duel["turn"]
    ]

    if user_id != current:

        return (
            f"아직 "
            f"{duel['names'][current]}의 "
            "차례야!"
        )

    if action == "hit":

        if not duel["deck"]:
            duel["deck"] = blackjack_new_deck()

        duel["cards"][user_id].append(
            duel["deck"].pop()
        )

        score = blackjack_score(
            duel["cards"][user_id]
        )

        if score > 21:

            duel["stood"].add(
                user_id
            )

            message = (
                f"💥 "
                f"{duel['names'][user_id]} "
                f"버스트! "
                f"({blackjack_score_text(duel['cards'][user_id])})\n\n"
            )

            if len(duel["stood"]) == 2:

                result = blackjack_duel_result(
                    duel
                )

                BLACKJACK_DUELS.pop(
                    group_id,
                    None
                )

                return message + result

            return (
                message
                + blackjack_duel_advance(
                    duel
                )
            )

        if score == 21:

            duel["stood"].add(
                user_id
            )

            message = (
                f"🎯 "
                f"{duel['names'][user_id]} "
                "21 도달! "
                "자동으로 스탠드했어.\n\n"
            )

            if len(duel["stood"]) == 2:

                result = blackjack_duel_result(
                    duel
                )

                BLACKJACK_DUELS.pop(
                    group_id,
                    None
                )

                return message + result

            return (
                message
                + blackjack_duel_advance(
                    duel
                )
            )

        return (
            f"🃏 "
            f"{duel['names'][user_id]} 히트!\n"
            f"합계: "
            f"{blackjack_score_text(duel['cards'][user_id])}\n\n"
            + blackjack_duel_board(
                duel
            )
        )

    duel["stood"].add(user_id)

    message = (
        f"✋ "
        f"{duel['names'][user_id]} "
        "스탠드!\n\n"
    )

    if len(duel["stood"]) == 2:

        result = blackjack_duel_result(
            duel
        )

        BLACKJACK_DUELS.pop(
            group_id,
            None
        )

        return message + result

    return (
        message
        + blackjack_duel_advance(
            duel
        )
    )


# =========================================================
# Webhook
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return "LINE BOT OK", 200


@app.route("/webhook", methods=["POST"])
def webhook():

    body = request.get_data()

    signature = request.headers.get(
        "X-Line-Signature",
        ""
    )

    if not verify_signature(
        body,
        signature
    ):
        abort(400)

    try:

        data = request.get_json()

    except Exception:

        abort(400)

    events = data.get(
        "events",
        []
    )

    for event in events:

        if event.get("type") != "message":
            continue

        message = event.get(
            "message",
            {}
        )

        if message.get("type") != "text":
            continue

        text = message.get(
            "text",
            ""
        ).strip()

        if not text:
            continue

        source = event.get(
            "source",
            {}
        )

        group_id = source.get(
            "groupId"
        )

        user_id = source.get(
            "userId"
        )

        reply_token = event.get(
            "replyToken"
        )

        if not group_id:
            continue

        event_time = get_event_datetime(
            event
        )

        # -------------------------------------------------
        # 프로필
        # -------------------------------------------------

        user_name = get_profile_name(
            user_id,
            group_id
        )

        # -------------------------------------------------
        # 닉네임
        # -------------------------------------------------

        formatted = make_name_format(
            text
        )

        if formatted:

            reply_message(
                reply_token,
                formatted
            )

            continue

        # -------------------------------------------------
        # ! AI
        # -------------------------------------------------

        if text.startswith("!"):

            prompt = text[1:].strip()

            if prompt:

                answer = answer_ai_question(
                    prompt
                )

                reply_message(
                    reply_token,
                    answer
                )

            continue

        # -------------------------------------------------
        # 명령어
        # -------------------------------------------------

        normalized = re.sub(
            r"\s+",
            "",
            text
        )

        # -------------------------------------------------
        # 유저 vs 유저 블랙잭 신청
        # -------------------------------------------------

        duel_match = re.match(
            r"^블랙잭\s+(.+)$",
            text
        )

        if (
            duel_match
            and normalized not in [
                "블랙잭종료",
                "종료블랙잭"
            ]
        ):

            target_name = (
                duel_match
                .group(1)
                .strip()
            )

            reply_message(
                reply_token,
                blackjack_duel_challenge(
                    group_id,
                    user_id,
                    user_name,
                    target_name
                )
            )

            continue

        # -------------------------------------------------
        # 수락
        # -------------------------------------------------

        if normalized == "수락":

            reply_message(
                reply_token,
                blackjack_duel_accept(
                    group_id,
                    user_id,
                    user_name
                )
            )

            continue

        # -------------------------------------------------
        # 거절
        # -------------------------------------------------

        if normalized == "거절":

            reply_message(
                reply_token,
                blackjack_duel_decline(
                    group_id,
                    user_id,
                    user_name
                )
            )

            continue

        # -------------------------------------------------
        # 대결 취소
        # -------------------------------------------------

        if normalized in [
            "대결취소",
            "블랙잭대결취소"
        ]:

            challenge = (
                BLACKJACK_CHALLENGES.get(
                    group_id
                )
            )

            if (
                challenge
                and
                challenge["challenger_id"]
                == user_id
            ):

                BLACKJACK_CHALLENGES.pop(
                    group_id,
                    None
                )

                reply_message(
                    reply_token,
                    "신청한 블랙잭 대결을 취소했어."
                )

            else:

                reply_message(
                    reply_token,
                    "취소할 네 대결 신청이 없어."
                )

            continue

        # -------------------------------------------------
        # 유저 vs 유저 히트
        # -------------------------------------------------

        if normalized in [
            "히트",
            "카드더",
            "한장더"
        ]:

            duel = BLACKJACK_DUELS.get(
                group_id
            )

            if duel:

                reply_message(
                    reply_token,
                    blackjack_duel_action(
                        group_id,
                        user_id,
                        "hit"
                    )
                )

                continue

        # -------------------------------------------------
        # 유저 vs 유저 스탠드
        # -------------------------------------------------

        if normalized in [
            "스탠드",
            "스탑",
            "멈춤"
        ]:

            duel = BLACKJACK_DUELS.get(
                group_id
            )

            if duel:

                reply_message(
                    reply_token,
                    blackjack_duel_action(
                        group_id,
                        user_id,
                        "stand"
                    )
                )

                continue

        # -------------------------------------------------
        # 1인 블랙잭 종료
        # -------------------------------------------------

        if normalized in [
            "블랙잭종료",
            "종료블랙잭"
        ]:

            reply_message(
                reply_token,
                blackjack_cancel(
                    group_id,
                    user_id
                )
            )

            continue

        # -------------------------------------------------
        # 1인 블랙잭 시작
        # -------------------------------------------------

        if normalized in [
            "블랙잭",
            "블랙잭시작"
        ]:

            reply_message(
                reply_token,
                blackjack_start(
                    group_id,
                    user_id,
                    user_name
                )
            )

            continue

        # -------------------------------------------------
        # 1인 블랙잭 히트
        # -------------------------------------------------

        if normalized in [
            "히트",
            "카드더",
            "한장더"
        ]:

            reply_message(
                reply_token,
                blackjack_hit(
                    group_id,
                    user_id,
                    user_name
                )
            )

            continue

        # -------------------------------------------------
        # 1인 블랙잭 스탠드
        # -------------------------------------------------

        if normalized in [
            "스탠드",
            "스탑",
            "멈춤"
        ]:

            reply_message(
                reply_token,
                blackjack_stand(
                    group_id,
                    user_id,
                    user_name
                )
            )

            continue

        # -------------------------------------------------
        # 소통량 초기화
        # -------------------------------------------------

        if normalized in [
            "단라소통량초기화",
            "소통량초기화"
        ]:

            success = reset_today(
                group_id
            )

            if success:

                reply_message(
                    reply_token,
                    "✅ 오늘 소통량을 초기화했어."
                )

            else:

                reply_message(
                    reply_token,
                    "❌ 소통량 초기화에 실패했어."
                )

            continue

        # -------------------------------------------------
        # 단라 소통량
        # -------------------------------------------------

        if normalized in [
            "단라소통량",
            "소통량"
        ]:

            reply_message(
                reply_token,
                make_ranking_text(
                    group_id
                )
            )

            continue

        # -------------------------------------------------
        # 내 마딧수
        # -------------------------------------------------

        if normalized in [
            "내마딧수",
            "내소통량",
            "내말딧수"
        ]:

            reply_message(
                reply_token,
                make_my_count_text(
                    group_id,
                    user_id,
                    user_name
                )
            )

            continue

        # -------------------------------------------------
        # 방 통계
        # -------------------------------------------------

        if normalized in [
            "방통계",
            "방통계보기"
        ]:

            reply_message(
                reply_token,
                make_room_stats(
                    group_id
                )
            )

            continue

        # -------------------------------------------------
        # 내 통계
        # -------------------------------------------------

        if normalized in [
            "내통계",
            "내통계보기"
        ]:

            reply_message(
                reply_token,
                make_my_stats(
                    group_id,
                    user_id,
                    user_name
                )
            )

            continue

        # -------------------------------------------------
        # 일반 메시지
        # -------------------------------------------------

        if is_laughter_only(text):
            continue

        saved = save_chat_message(
            group_id,
            user_id,
            user_name,
            text,
            event_time
        )

        if saved:

            # 랜덤 AI
            maybe_start_random_ai(
                group_id,
                reply_token
            )

    return "OK", 200


# =========================================================
# 실행
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
