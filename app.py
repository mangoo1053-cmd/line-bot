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

# 랜덤 AI 개입 확률
AI_TRIGGER_CHANCE = 0.03

# 랜덤 AI 개입 쿨다운
AI_COOLDOWN_SECONDS = 3 * 60 * 60

# 최근 대화 몇 개를 AI에게 보여줄지
RECENT_MESSAGE_LIMIT = 12


# =========================================================
# 일반 질문용 모델
# =========================================================

# 일반적인 !질문은 빠른 모델을 우선 사용
NORMAL_GEMINI_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
]


# =========================================================
# 실시간 검색용 모델
# =========================================================

SEARCH_GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
]


# =========================================================
# 시간 제한
# =========================================================

# 일반 질문
NORMAL_AI_TIMEOUT = 8

# Google 검색 질문
SEARCH_AI_TIMEOUT = 15


# =========================================================
# 기본 페이지
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running!", 200


# =========================================================
# Supabase
# =========================================================

def supabase_request(method, table, params=None, data=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

    if method.upper() in ["POST", "PATCH", "DELETE"]:
        headers["Prefer"] = "return=representation"

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=data,
            timeout=10,
        )

        if response.status_code >= 400:
            print(
                "SUPABASE ERROR:",
                response.status_code,
                response.text
            )

        return response

    except Exception as e:
        print("SUPABASE REQUEST ERROR:", e)
        return None


# =========================================================
# LINE API
# =========================================================

def line_api(endpoint, payload):
    url = f"https://api.line.me/v2/bot/{endpoint}"

    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10,
        )

        print(
            "LINE API:",
            endpoint,
            response.status_code
        )

        if response.status_code >= 400:
            print(response.text)

        return response

    except Exception as e:
        print("LINE API ERROR:", e)
        return None


def reply_message(reply_token, text):
    if not reply_token:
        return

    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": text[:5000],
            }
        ],
    }

    line_api(
        "message/reply",
        payload
    )


# =========================================================
# LINE 사용자 이름
# =========================================================

def get_profile_name(user_id):
    url = (
        f"https://api.line.me/v2/bot/profile/"
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
            timeout=5,
        )

        if response.status_code == 200:
            data = response.json()

            return data.get(
                "displayName",
                "알 수 없음"
            )

    except Exception as e:
        print("PROFILE ERROR:", e)

    return "알 수 없음"


# =========================================================
# 웃음만 있는 메시지
# =========================================================

def is_laugh_only(text):
    if not text:
        return False

    text = text.strip()

    return bool(
        re.fullmatch(
            r"[ㅋㅎㅠㅜ]+",
            text
        )
    )


# =========================================================
# 메시지 저장
# =========================================================

def save_message(
    group_id,
    user_id,
    user_name,
    message
):
    now = datetime.now(KST)

    data = {
        "group_id": group_id,
        "user_id": user_id,
        "user_name": user_name,
        "message": message,
        "date_kst":
            now.date().isoformat(),
        "hour_kst":
            now.hour,
    }

    return supabase_request(
        "POST",
        "chat_messages",
        data=data
    )


# =========================================================
# 오늘 내 소통량
# =========================================================

def get_today_count(
    group_id,
    user_id
):
    today = (
        datetime.now(KST)
        .date()
        .isoformat()
    )

    response = supabase_request(
        "GET",
        "chat_messages",
        params={
            "select": "id",
            "group_id": f"eq.{group_id}",
            "user_id": f"eq.{user_id}",
            "date_kst": f"eq.{today}",
        }
    )

    if not response:
        return 0

    if response.status_code >= 400:
        return 0

    try:
        return len(
            response.json()
        )
    except:
        return 0


# =========================================================
# 오늘 소통량 순위
# =========================================================

def get_today_ranking(group_id):
    today = (
        datetime.now(KST)
        .date()
        .isoformat()
    )

    response = supabase_request(
        "GET",
        "chat_messages",
        params={
            "select":
                "user_id,user_name",
            "group_id":
                f"eq.{group_id}",
            "date_kst":
                f"eq.{today}",
            "order":
                "created_at.asc",
        }
    )

    if not response:
        return []

    if response.status_code >= 400:
        return []

    try:
        messages = response.json()
    except:
        return []

    users = {}

    for item in messages:
        user_id = item.get(
            "user_id"
        )

        user_name = item.get(
            "user_name",
            "알 수 없음"
        )

        if user_id not in users:
            users[user_id] = {
                "name": user_name,
                "count": 0,
            }

        users[user_id]["count"] += 1

    ranking = list(
        users.values()
    )

    ranking.sort(
        key=lambda x: x["count"],
        reverse=True
    )

    return ranking


# =========================================================
# 방 통계
# =========================================================

def get_room_stats(group_id):
    response = supabase_request(
        "GET",
        "chat_messages",
        params={
            "select":
                "user_id,date_kst",
            "group_id":
                f"eq.{group_id}",
        }
    )

    if not response:
        return {
            "total": 0,
            "people": 0,
            "active_day": "없음",
        }

    if response.status_code >= 400:
        return {
            "total": 0,
            "people": 0,
            "active_day": "없음",
        }

    try:
        messages = response.json()
    except:
        messages = []

    total = len(messages)

    people = set()
    days = {}

    for item in messages:
        user_id = item.get(
            "user_id"
        )

        date = item.get(
            "date_kst"
        )

        if user_id:
            people.add(user_id)

        if date:
            days[date] = (
                days.get(date, 0) + 1
            )

    active_day = "없음"

    if days:
        active_day = max(
            days,
            key=days.get
        )

    return {
        "total": total,
        "people": len(people),
        "active_day": active_day,
    }


# =========================================================
# 개인 통계
# =========================================================

def get_personal_stats(
    group_id,
    user_id
):
    response = supabase_request(
        "GET",
        "chat_messages",
        params={
            "select":
                "user_id,user_name,hour_kst",
            "group_id":
                f"eq.{group_id}",
        }
    )

    if not response:
        return {
            "total": 0,
            "rank": 0,
            "active_hour": "없음",
        }

    if response.status_code >= 400:
        return {
            "total": 0,
            "rank": 0,
            "active_hour": "없음",
        }

    try:
        messages = response.json()
    except:
        messages = []

    users = {}
    hours = {}

    for item in messages:
        uid = item.get(
            "user_id"
        )

        hour = item.get(
            "hour_kst"
        )

        if uid:
            users[uid] = (
                users.get(uid, 0) + 1
            )

        if uid == user_id:
            if hour is not None:
                hours[hour] = (
                    hours.get(hour, 0) + 1
                )

    my_total = users.get(
        user_id,
        0
    )

    sorted_users = sorted(
        users.items(),
        key=lambda x: x[1],
        reverse=True
    )

    rank = 0

    for i, (uid, count) in enumerate(
        sorted_users,
        start=1
    ):
        if uid == user_id:
            rank = i
            break

    active_hour = "없음"

    if hours:
        active_hour = max(
            hours,
            key=hours.get
        )

    return {
        "total": my_total,
        "rank": rank,
        "active_hour":
            active_hour,
    }


# =========================================================
# 슈퍼스크립트
# =========================================================

SUPERSCRIPT = {
    "0": "⁰",
    "1": "¹",
    "2": "²",
    "3": "³",
    "4": "⁴",
    "5": "⁵",
    "6": "⁶",
    "7": "⁷",
    "8": "⁸",
    "9": "⁹",
}


def to_superscript(number):
    return "".join(
        SUPERSCRIPT.get(
            char,
            char
        )
        for char in str(number)
    )


# =========================================================
# 닉네임
# =========================================================

def make_nickname(text):
    parts = text.strip().split()

    if len(parts) != 4:
        return None

    name = parts[0]
    gender = parts[1]
    age = parts[2]
    kind = parts[3]

    if not re.fullmatch(
        r"\d{1,2}",
        age
    ):
        return None

    age = age.zfill(2)

    if kind == "돔":
        letter = "𝒅"

    elif kind == "섭":
        letter = "𝒔"

    elif kind == "스위치":
        letter = "𝒔/𝒅"

    elif kind == "바닐라":
        letter = "𝒗"

    else:
        return None

    if gender == "여자":
        emoji = "📡"

    elif gender == "남자":
        emoji = "🔭"

    else:
        return None

    return (
        f"{name}"
        f"{letter}"
        f"{to_superscript(age)}"
        f"{emoji}"
    )


# =========================================================
# 최근 대화
# =========================================================

def get_recent_messages(
    group_id,
    limit=RECENT_MESSAGE_LIMIT
):
    response = supabase_request(
        "GET",
        "chat_messages",
        params={
            "select":
                "user_name,message,created_at",
            "group_id":
                f"eq.{group_id}",
            "order":
                "created_at.desc",
            "limit":
                str(limit),
        }
    )

    if not response:
        return []

    if response.status_code >= 400:
        return []

    try:
        messages = response.json()
    except:
        return []

    messages.reverse()

    return messages


# =========================================================
# 실시간 검색 여부
# =========================================================

def needs_realtime_search(prompt):
    text = prompt.lower().strip()

    realtime_keywords = [

        # 시간
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

        # 최신 정보
        "최신",
        "최근",
        "요즘",
        "업데이트",

        # 뉴스
        "뉴스",
        "속보",
        "사건",
        "사고",

        # 날씨
        "날씨",
        "기온",
        "미세먼지",
        "비 와",
        "비와",
        "눈 와",
        "눈와",

        # 스포츠
        "경기 결과",
        "경기결과",
        "경기 일정",
        "경기일정",
        "스코어",
        "현재 순위",

        # 가격/금융
        "환율",
        "주가",
        "주식",
        "코인",
        "비트코인",
        "가격",
        "시세",

        # 일정
        "일정",
        "예매",
        "예약",

        # 직접 검색 요청
        "검색해",
        "검색해서",
        "찾아줘",
        "찾아봐",
        "인터넷에서",
        "웹에서",
    ]

    for keyword in realtime_keywords:
        if keyword in text:
            return True

    return False


# =========================================================
# Gemini 공통 요청
# =========================================================

def call_gemini_model(
    model,
    prompt,
    use_google_search=False,
    timeout=8
):
    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    params = {
        "key": GEMINI_API_KEY
    }

    system_instruction = """
너는 LINE 단체 채팅방에서 사용하는 AI야.

사용자의 질문에 자연스럽게 답해.

규칙:
- 한국어로 답해
- 친구처럼 자연스럽게 말해
- 너무 길게 설명하지 마
- 핵심부터 답해
- 필요할 때만 자세하게 설명해
- 모르는 내용은 아는 척하지 마
- 불필요하게 'AI입니다'라고 하지 마
"""

    payload = {
        "system_instruction": {
            "parts": [
                {
                    "text":
                        system_instruction
                }
            ]
        },

        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ],

        "generationConfig": {
            "temperature": 0.8,
            "maxOutputTokens": 350,
        }
    }

    # 실시간 검색이 필요한 경우에만 검색 도구 사용
    if use_google_search:
        payload["tools"] = [
            {
                "google_search": {}
            }
        ]

    try:
        start = time.time()

        response = requests.post(
            url,
            params=params,
            json=payload,
            timeout=timeout,
        )

        elapsed = round(
            time.time() - start,
            2
        )

        print(
            f"GEMINI {model} "
            f"status={response.status_code} "
            f"time={elapsed}s "
            f"search={use_google_search}"
        )

        if response.status_code == 200:

            data = response.json()

            candidates = data.get(
                "candidates",
                []
            )

            if not candidates:
                print(
                    f"GEMINI {model}: "
                    "no candidates"
                )
                return None

            content = candidates[0].get(
                "content",
                {}
            )

            parts = content.get(
                "parts",
                []
            )

            result = ""

            for part in parts:
                if "text" in part:
                    result += part["text"]

            result = result.strip()

            if result:
                return result

            return None

        # 429 = 요청 제한
        if response.status_code == 429:
            print(
                f"GEMINI {model}: 429 rate limit"
            )
            return None

        # 503 = 일시적 unavailable
        if response.status_code == 503:
            print(
                f"GEMINI {model}: 503 unavailable"
            )
            return None

        print(
            f"GEMINI ERROR {model}:",
            response.status_code,
            response.text[:1000]
        )

        return None

    except requests.exceptions.Timeout:
        print(
            f"GEMINI TIMEOUT: {model}"
        )
        return None

    except Exception as e:
        print(
            f"GEMINI EXCEPTION {model}:",
            e
        )
        return None


# =========================================================
# 일반 AI 질문
# =========================================================

def ask_normal_gemini(prompt):

    if not GEMINI_API_KEY:
        return None

    for model in NORMAL_GEMINI_MODELS:

        result = call_gemini_model(
            model=model,
            prompt=prompt,
            use_google_search=False,
            timeout=NORMAL_AI_TIMEOUT,
        )

        if result:
            print(
                "NORMAL AI SUCCESS:",
                model
            )

            return result

        # 오래 기다리지 않고 다음 모델
        print(
            "NORMAL AI FALLBACK:",
            model
        )

    return None


# =========================================================
# 실시간 AI 질문
# =========================================================

def ask_search_gemini(prompt):

    if not GEMINI_API_KEY:
        return None

    for model in SEARCH_GEMINI_MODELS:

        result = call_gemini_model(
            model=model,
            prompt=prompt,
            use_google_search=True,
            timeout=SEARCH_AI_TIMEOUT,
        )

        if result:
            print(
                "SEARCH AI SUCCESS:",
                model
            )

            return result

        print(
            "SEARCH AI FALLBACK:",
            model
        )

    return None


# =========================================================
# ! 질문 처리
# =========================================================

def answer_exclamation_question(prompt):

    prompt = prompt.strip()

    if not prompt:
        return "질문을 입력해줘."

    realtime = needs_realtime_search(
        prompt
    )

    print(
        "AI QUESTION:",
        prompt,
        "| realtime:",
        realtime
    )

    # 일반 질문
    if not realtime:
        return ask_normal_gemini(
            prompt
        )

    # 실시간 질문
    return ask_search_gemini(
        prompt
    )


# =========================================================
# 마지막 랜덤 AI 답변
# =========================================================

def get_last_ai_reply(group_id):

    response = supabase_request(
        "GET",
        "bot_state",
        params={
            "select":
                "last_ai_reply",
            "group_id":
                f"eq.{group_id}",
            "limit": "1",
        }
    )

    if not response:
        return None

    if response.status_code >= 400:
        return None

    try:
        data = response.json()

        if not data:
            return None

        value = data[0].get(
            "last_ai_reply"
        )

        if not value:
            return None

        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        )

    except Exception as e:
        print(
            "LAST AI TIME ERROR:",
            e
        )

        return None


def update_last_ai_reply(group_id):

    now = (
        datetime.now(
            timezone.utc
        )
        .isoformat()
    )

    response = supabase_request(
        "GET",
        "bot_state",
        params={
            "select":
                "group_id",
            "group_id":
                f"eq.{group_id}",
            "limit": "1",
        }
    )

    exists = False

    if response and response.status_code < 400:

        try:
            exists = (
                len(
                    response.json()
                ) > 0
            )
        except:
            exists = False

    if exists:

        supabase_request(
            "PATCH",
            "bot_state",
            params={
                "group_id":
                    f"eq.{group_id}"
            },
            data={
                "last_ai_reply": now
            }
        )

    else:

        supabase_request(
            "POST",
            "bot_state",
            data={
                "group_id":
                    group_id,
                "last_ai_reply":
                    now
            }
        )


# =========================================================
# 랜덤 AI 개입
# =========================================================

def maybe_ai_intervene(group_id):

    if random.random() > AI_TRIGGER_CHANCE:
        return None

    last_reply = get_last_ai_reply(
        group_id
    )

    if last_reply:

        now = datetime.now(
            timezone.utc
        )

        elapsed = (
            now - last_reply
        ).total_seconds()

        if elapsed < AI_COOLDOWN_SECONDS:
            return None

    recent = get_recent_messages(
        group_id,
        RECENT_MESSAGE_LIMIT
    )

    if not recent:
        return None

    conversation = []

    for item in recent:

        name = item.get(
            "user_name",
            "알 수 없음"
        )

        message = item.get(
            "message",
            ""
        )

        conversation.append(
            f"{name}: {message}"
        )

    conversation_text = "\n".join(
        conversation
    )

    prompt = f"""
다음은 LINE 단체 채팅방의 최근 대화야.

{conversation_text}

너는 이 채팅방의 평범한 참가자야.

지금 대화에 자연스럽게 끼어들 만한 상황인지 판단해.

정말 끼어들 이유가 있을 때:

YES|짧은 답변

끼어들 필요가 없으면:

NO

규칙:
- 억지로 끼어들지 마
- 아무 말이나 하지 마
- 너무 자주 말하지 마
- 정치, 종교, 성적인 내용, 개인정보,
  심각한 싸움이나 민감한 주제에는 개입하지 마
- 친구처럼 자연스럽게 말해
- 짧게 답해
"""

    result = ask_normal_gemini(
        prompt
    )

    if not result:
        return None

    result = result.strip()

    if result.upper() == "NO":
        return None

    if result.upper().startswith(
        "YES|"
    ):

        reply = result[4:].strip()

        if reply:

            update_last_ai_reply(
                group_id
            )

            return reply

    return None


# =========================================================
# 오늘 소통량 초기화
# =========================================================

def reset_today(group_id):

    today = (
        datetime.now(KST)
        .date()
        .isoformat()
    )

    response = supabase_request(
        "DELETE",
        "chat_messages",
        params={
            "group_id":
                f"eq.{group_id}",
            "date_kst":
                f"eq.{today}",
        }
    )

    if response and response.status_code < 400:
        return True

    return False


# =========================================================
# Webhook
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook():

    body = request.get_data(
        as_text=True
    )

    signature = request.headers.get(
        "X-Line-Signature",
        ""
    )

    if not signature:
        abort(400)

    hash_value = hmac.new(
        CHANNEL_SECRET.encode(
            "utf-8"
        ),
        body.encode(
            "utf-8"
        ),
        hashlib.sha256
    ).digest()

    expected_signature = (
        base64.b64encode(
            hash_value
        )
        .decode("utf-8")
    )

    if not hmac.compare_digest(
        signature,
        expected_signature
    ):
        abort(400)

    try:
        data = json.loads(body)

    except:
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

        source = event.get(
            "source",
            {}
        )

        # 그룹방만
        if source.get(
            "type"
        ) != "group":
            continue

        group_id = source.get(
            "groupId"
        )

        user_id = source.get(
            "userId"
        )

        reply_token = event.get(
            "replyToken"
        )

        if not group_id or not user_id:
            continue

        # -------------------------------------------------
        # 사용자 이름
        # -------------------------------------------------

        user_name = get_profile_name(
            user_id
        )

        # =================================================
        # 닉네임 생성
        # =================================================

        nickname = make_nickname(
            text
        )

        if nickname:

            reply_message(
                reply_token,
                nickname
            )

            continue

        # =================================================
        # 명령어 공백 제거 버전
        # =================================================

        normalized = text.replace(
            " ",
            ""
        )

        # =================================================
        # 내 마딧수
        # =================================================

        if normalized in [
            "내마딧수",
            "내소통량"
        ]:

            count = get_today_count(
                group_id,
                user_id
            )

            reply_message(
                reply_token,
                f"{user_name}님의 오늘 "
                f"소통량은 {count}개야."
            )

            continue

        # =================================================
        # 단라 소통량
        # =================================================

        if normalized == "단라소통량":

            ranking = get_today_ranking(
                group_id
            )

            if not ranking:

                reply_message(
                    reply_token,
                    "오늘 아직 소통량이 없어."
                )

                continue

            result = (
                "🏆 단라 소통량 순위\n\n"
            )

            for i, item in enumerate(
                ranking[:20],
                start=1
            ):

                result += (
                    f"{i}위 "
                    f"{item['name']} "
                    f"{item['count']}개\n"
                )

            reply_message(
                reply_token,
                result
            )

            continue

        # =================================================
        # 단라 소통량 초기화
        # =================================================

        if normalized == "단라소통량초기화":

            success = reset_today(
                group_id
            )

            if success:

                reply_message(
                    reply_token,
                    "오늘 소통량을 초기화했어."
                )

            else:

                reply_message(
                    reply_token,
                    "초기화 중 오류가 발생했어."
                )

            continue

        # =================================================
        # 방 통계
        # =================================================

        if normalized == "방통계":

            stats = get_room_stats(
                group_id
            )

            result = (
                "📊 방 통계\n\n"
                f"총 메시지: "
                f"{stats['total']}개\n"
                f"참여 인원: "
                f"{stats['people']}명\n"
                f"가장 활발했던 날: "
                f"{stats['active_day']}"
            )

            reply_message(
                reply_token,
                result
            )

            continue

        # =================================================
        # 내 통계
        # =================================================

        if normalized == "내통계":

            stats = get_personal_stats(
                group_id,
                user_id
            )

            if stats["rank"] == 0:
                rank_text = "순위 없음"

            else:
                rank_text = (
                    f"{stats['rank']}위"
                )

            if stats["active_hour"] == "없음":
                hour_text = "없음"

            else:
                hour_text = (
                    f"{stats['active_hour']}시"
                )

            result = (
                f"📊 {user_name}님의 통계\n\n"
                f"총 소통량: "
                f"{stats['total']}개\n"
                f"방 내 순위: "
                f"{rank_text}\n"
                f"가장 활발한 시간: "
                f"{hour_text}"
            )

            reply_message(
                reply_token,
                result
            )

            continue

        # =================================================
        # ! AI 질문
        # =================================================

        if text.startswith("!"):

            prompt = text[1:].strip()

            answer = (
                answer_exclamation_question(
                    prompt
                )
            )

            if answer:

                reply_message(
                    reply_token,
                    answer
                )

            else:

                reply_message(
                    reply_token,
                    "지금 AI가 응답하지 못했어. "
                    "잠시 후 다시 물어봐줘."
                )

            # ! 질문은 소통량에 넣지 않음
            continue

        # =================================================
        # 일반 메시지
        # =================================================

        if is_laugh_only(text):
            continue

        save_message(
            group_id,
            user_id,
            user_name,
            text
        )

        # =================================================
        # 랜덤 AI 개입
        # =================================================

        ai_reply = maybe_ai_intervene(
            group_id
        )

        if ai_reply:

            reply_message(
                reply_token,
                ai_reply
            )

    return "OK", 200


# =========================================================
# 실행
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
