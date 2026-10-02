from flask import Flask, request, abort
import requests
import hashlib
import hmac
import base64
import json
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

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


# =========================================================
# 기본 설정
# =========================================================

KST = ZoneInfo("Asia/Seoul")

AI_TRIGGER_CHANCE = 0.03

AI_COOLDOWN_SECONDS = 3 * 60 * 60

RECENT_MESSAGE_LIMIT = 12


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
# 시간
# =========================================================

def now_kst():
    return datetime.now(KST)


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()


# =========================================================
# LINE 서명 확인
# =========================================================

def verify_signature(body, signature):
    if not CHANNEL_SECRET:
        return False

    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()

    expected = base64.b64encode(digest).decode("utf-8")

    return hmac.compare_digest(
        expected,
        signature or ""
    )


# =========================================================
# LINE API
# =========================================================

def line_api(endpoint, payload):
    url = f"https://api.line.me/v2/bot/{endpoint}"

    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10
        )

        print(
            f"LINE API: {endpoint} "
            f"{response.status_code}"
        )

        if response.status_code >= 400:
            print(
                "LINE ERROR:",
                response.text[:1000]
            )

        return response

    except Exception as e:
        print(
            "LINE API ERROR:",
            repr(e)
        )

        return None


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
# LINE 프로필
# =========================================================

def get_profile_name(user_id):
    if not user_id:
        return "알 수 없음"

    url = (
        "https://api.line.me/v2/bot/profile/"
        f"{user_id}"
    )

    headers = {
        "Authorization":
            f"Bearer {CHANNEL_ACCESS_TOKEN}"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=5
        )

        if response.status_code == 200:
            data = response.json()

            return data.get(
                "displayName",
                "알 수 없음"
            )

    except Exception as e:
        print(
            "PROFILE ERROR:",
            repr(e)
        )

    return "알 수 없음"


# =========================================================
# Supabase
# =========================================================

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }


def supabase_get(table, params=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []

    try:
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=supabase_headers(),
            params=params or {},
            timeout=6
        )

        if response.status_code >= 400:
            print(
                "SUPABASE GET ERROR:",
                response.status_code,
                response.text[:1000]
            )

            return []

        return response.json()

    except Exception as e:
        print(
            "SUPABASE GET ERROR:",
            repr(e)
        )

        return []


def supabase_post(table, data):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("SUPABASE 설정 없음")
        return None

    headers = supabase_headers()

    headers["Prefer"] = "return=minimal"

    try:
        response = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            json=data,
            timeout=6
        )

        if response.status_code >= 400:
            print(
                "SUPABASE POST ERROR:",
                response.status_code,
                response.text[:1000]
            )

        return response

    except Exception as e:
        print(
            "SUPABASE POST ERROR:",
            repr(e)
        )

        return None


def supabase_patch(table, params, data):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None

    headers = supabase_headers()

    headers["Prefer"] = "return=minimal"

    try:
        response = requests.patch(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers,
            params=params,
            json=data,
            timeout=6
        )

        return response

    except Exception as e:
        print(
            "SUPABASE PATCH ERROR:",
            repr(e)
        )

        return None


def supabase_delete(table, params):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None

    try:
        response = requests.delete(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers=supabase_headers(),
            params=params,
            timeout=6
        )

        if response.status_code >= 400:
            print(
                "SUPABASE DELETE ERROR:",
                response.status_code,
                response.text[:1000]
            )

        return response

    except Exception as e:
        print(
            "SUPABASE DELETE ERROR:",
            repr(e)
        )

        return None


# =========================================================
# LINE 실제 메시지 시각 → 한국 날짜
# =========================================================

def get_event_datetime(event):
    """
    LINE event의 timestamp를 사용한다.

    서버가 실제로 이벤트를 처리한 시간이 아니라
    사용자가 메시지를 보낸 실제 시각을 기준으로 한다.
    """

    timestamp = event.get("timestamp")

    if timestamp is None:
        return now_kst()

    try:
        dt = datetime.fromtimestamp(
            int(timestamp) / 1000,
            tz=timezone.utc
        )

        return dt.astimezone(KST)

    except Exception as e:
        print(
            "EVENT TIME ERROR:",
            repr(e)
        )

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
        "date_kst": event_time.strftime(
            "%Y-%m-%d"
        ),
        "hour_kst": event_time.hour
    }

    response = supabase_post(
        "chat_messages",
        data
    )

    if response is not None and response.status_code < 300:

        print(
            "CHAT SAVED:",
            event_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            user_name,
            message[:50]
        )

        return True

    print(
        "CHAT SAVE FAILED:",
        event_time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        user_name
    )

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
# 소통량 조회
# =========================================================

def get_today_messages(group_id):

    today = now_kst().strftime(
        "%Y-%m-%d"
    )

    return supabase_get(
        "chat_messages",
        {
            "group_id":
                f"eq.{group_id}",

            "date_kst":
                f"eq.{today}",

            "select":
                "*",

            "order":
                "created_at.asc"
        }
    )


def get_all_group_messages(group_id):

    return supabase_get(
        "chat_messages",
        {
            "group_id":
                f"eq.{group_id}",

            "select":
                "*",

            "order":
                "created_at.asc"
        }
    )


# =========================================================
# 단라 소통량
# =========================================================

def today_ranking(group_id):

    rows = get_today_messages(
        group_id
    )

    counts = {}
    names = {}

    for row in rows:

        user_id = row.get(
            "user_id"
        )

        name = row.get(
            "user_name",
            "알 수 없음"
        )

        if not user_id:
            continue

        counts[user_id] = (
            counts.get(user_id, 0) + 1
        )

        names[user_id] = name

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
        return "오늘은 아직 소통량이 없어!"

    result = (
        "🏆 오늘 단라 소통량 순위\n\n"
    )

    for index, (
        user_id,
        count
    ) in enumerate(
        ranking[:20],
        start=1
    ):

        name = names.get(
            user_id,
            "알 수 없음"
        )

        result += (
            f"{index}위 "
            f"{name} — "
            f"{count}개\n"
        )

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

    count = 0

    for row in rows:

        if row.get(
            "user_id"
        ) == user_id:

            count += 1

    return (
        f"💬 {user_name}님의 "
        f"오늘 소통량\n"
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

    if (
        response is not None
        and response.status_code < 300
    ):
        return True

    return False


# =========================================================
# 이름 변환
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

    age_text = superscript_age(
        age
    )

    return (
        f"{name}"
        f"{kind_map[kind]}"
        f"{age_text}"
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

    result = []

    for row in rows:

        result.append(
            f"{row.get('user_name', '알 수 없음')}: "
            f"{row.get('message', '')}"
        )

    return "\n".join(result)


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

        print(
            "GEMINI_API_KEY 없음"
        )

        return None

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    params = {
        "key":
            GEMINI_API_KEY
    }

    body = {

        "contents": [
            {
                "parts": [
                    {
                        "text":
                            prompt
                    }
                ]
            }
        ],

        "generationConfig": {

            "temperature":
                0.8,

            "maxOutputTokens":
                300
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

        response = requests.post(
            url,
            params=params,
            json=body,
            timeout=timeout
        )

        elapsed = (
            time.time() - started
        )

        print(
            f"GEMINI {model} "
            f"status={response.status_code} "
            f"time={elapsed:.2f}s"
        )

        if response.status_code != 200:

            print(
                "GEMINI ERROR:",
                response.text[:1000]
            )

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

        texts = []

        for part in parts:

            if "text" in part:
                texts.append(
                    part["text"]
                )

        answer = "".join(
            texts
        ).strip()

        return (
            answer
            if answer
            else None
        )

    except Exception as e:

        print(
            f"GEMINI {model} ERROR:",
            repr(e)
        )

        return None


# =========================================================
# 실시간 질문 판별
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
  싸움이나 심각한 문제는 불필요하게 끼어들지 않는다.
- 이 요청은 일반 질문이므로 인터넷 검색을 하지 않는다.
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
# AI 질문 처리
# =========================================================

def answer_ai_question_async(
    group_id,
    prompt
):

    print(
        f"AI QUESTION: {prompt} | "
        f"realtime: "
        f"{needs_realtime_search(prompt)}"
    )

    if needs_realtime_search(
        prompt
    ):

        answer = ask_search_gemini(
            prompt
        )

    else:

        answer = ask_normal_gemini(
            prompt
        )

    push_message(
        group_id,
        answer
    )


def start_ai_question(
    group_id,
    prompt
):

    thread = threading.Thread(
        target=answer_ai_question_async,
        args=(
            group_id,
            prompt
        ),
        daemon=True
    )

    thread.start()


# =========================================================
# 랜덤 AI
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
        datetime.now(timezone.utc)
        - last
    ).total_seconds()

    return (
        elapsed >=
        AI_COOLDOWN_SECONDS
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


def ask_random_ai(group_id):

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

        if answer.upper().startswith(
            "YES|"
        ):

            reply = answer[4:].strip()

            if reply:

                save_last_ai_reply(
                    group_id
                )

                push_message(
                    group_id,
                    reply
                )

            return


def maybe_start_random_ai(
    group_id
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
        args=(group_id,),
        daemon=True
    )

    thread.start()


# =========================================================
# Webhook
# =========================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return "LINE BOT OK", 200


@app.route(
    "/webhook",
    methods=["POST"]
)
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

        if event.get(
            "type"
        ) != "message":

            continue

        message = event.get(
            "message",
            {}
        )

        if message.get(
            "type"
        ) != "text":

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

        # =================================================
        # LINE 실제 메시지 시간
        # =================================================

        event_time = get_event_datetime(
            event
        )

        print(
            "MESSAGE:",
            event_time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            text[:100]
        )

        # =================================================
        # 프로필
        # =================================================

        user_name = get_profile_name(
            user_id
        )

        # =================================================
        # 이름 변환
        # =================================================

        formatted = make_name_format(
            text
        )

        if formatted:

            reply_message(
                reply_token,
                formatted
            )

            continue

        # =================================================
        # AI 질문
        # =================================================

        if text.startswith("!"):

            prompt = text[1:].strip()

            if prompt:

                print(
                    "START ASYNC AI:",
                    prompt
                )

                start_ai_question(
                    group_id,
                    prompt
                )

            continue

        # =================================================
        # 명령어 판별
        # =================================================

        normalized = re.sub(
            r"\s+",
            "",
            text
        )

        # =================================================
        # 소통량 초기화
        # =================================================

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

        # =================================================
        # 단라 소통량
        # =================================================

        if normalized in [
            "단라소통량",
            "소통량"
        ]:

            result = make_ranking_text(
                group_id
            )

            reply_message(
                reply_token,
                result
            )

            continue

        # =================================================
        # 내 마딧수
        # =================================================

        if normalized in [
            "내마딧수",
            "내소통량",
            "내말딧수"
        ]:

            result = make_my_count_text(
                group_id,
                user_id,
                user_name
            )

            reply_message(
                reply_token,
                result
            )

            continue

        # =================================================
        # 방 통계
        # =================================================

        if normalized in [
            "방통계",
            "방통계보기"
        ]:

            result = make_room_stats(
                group_id
            )

            reply_message(
                reply_token,
                result
            )

            continue

        # =================================================
        # 내 통계
        # =================================================

        if normalized in [
            "내통계",
            "내통계보기"
        ]:

            result = make_my_stats(
                group_id,
                user_id,
                user_name
            )

            reply_message(
                reply_token,
                result
            )

            continue

        # =================================================
        # 일반 메시지 저장
        # =================================================

        if not is_laughter_only(
            text
        ):

            saved = save_chat_message(
                group_id,
                user_id,
                user_name,
                text,
                event_time
            )

            if saved:

                print(
                    "소통량 집계 완료:",
                    user_name,
                    event_time.strftime(
                        "%Y-%m-%d"
                    )
                )

            # 랜덤 AI
            maybe_start_random_ai(
                group_id
            )

    # =====================================================
    # LINE에 즉시 200
    # =====================================================

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
