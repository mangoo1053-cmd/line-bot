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
# 설정
# =========================================================

KST = ZoneInfo("Asia/Seoul")

# 일반 채팅 중 AI가 랜덤으로 개입할 확률
AI_TRIGGER_CHANCE = 0.03

# AI 랜덤 개입 후 3시간 동안 다시 개입하지 않음
AI_COOLDOWN_SECONDS = 3 * 60 * 60

# AI가 참고할 최근 메시지 수
RECENT_MESSAGE_LIMIT = 12

# Gemini 모델 fallback 순서
GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]


# =========================================================
# 기본
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "LINE BOT IS RUNNING", 200


# =========================================================
# Supabase
# =========================================================

def supabase_request(method, table, params=None, data=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Supabase 환경변수가 없습니다.")
        return None

    url = f"{SUPABASE_URL}/rest/v1/{table}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

    if method.upper() == "POST":
        headers["Prefer"] = "return=minimal"

    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=data,
            timeout=15
        )

        if response.status_code >= 400:
            print(
                f"Supabase 오류: {response.status_code} "
                f"{response.text}"
            )
            return None

        if response.text:
            try:
                return response.json()
            except Exception:
                return True

        return True

    except Exception as e:
        print("Supabase 연결 오류:", e)
        return None


# =========================================================
# LINE API
# =========================================================

def line_api(endpoint, method="POST", data=None):
    url = f"https://api.line.me/v2/bot/{endpoint}"

    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            json=data,
            timeout=15
        )

        if response.status_code >= 400:
            print(
                f"LINE API 오류: {response.status_code} "
                f"{response.text}"
            )
            return None

        if response.text:
            try:
                return response.json()
            except Exception:
                return True

        return True

    except Exception as e:
        print("LINE API 연결 오류:", e)
        return None


def reply_message(reply_token, text):
    return line_api(
        "message/reply",
        "POST",
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
# LINE 사용자 이름
# =========================================================

def get_profile_name(user_id):
    result = line_api(
        f"profile/{user_id}",
        "GET"
    )

    if result and isinstance(result, dict):
        return result.get("displayName", "알 수 없음")

    return "알 수 없음"


# =========================================================
# 웃음만 있는 메시지인지 확인
# =========================================================

def is_laugh_only(text):
    if not text:
        return True

    text = text.strip()

    if not text:
        return True

    # ㅋㅋ, ㅋㅋㅋㅋ, ㅎㅎ, ㅠㅠ 등만 있는 경우 제외
    return re.fullmatch(r"[ㅋㅎㅠㅜ]+", text) is not None


# =========================================================
# 메시지 저장
# =========================================================

def save_message(group_id, user_id, user_name, message):
    now = datetime.now(KST)

    data = {
        "group_id": group_id,
        "user_id": user_id,
        "user_name": user_name,
        "message": message,
        "date_kst": now.strftime("%Y-%m-%d"),
        "hour_kst": now.hour,
    }

    return supabase_request(
        "POST",
        "chat_messages",
        data=data
    )


# =========================================================
# 오늘 내 소통량
# =========================================================

def get_today_count(group_id, user_id):
    today = datetime.now(KST).strftime("%Y-%m-%d")

    params = {
        "group_id": f"eq.{group_id}",
        "user_id": f"eq.{user_id}",
        "date_kst": f"eq.{today}",
        "select": "id",
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if result is None:
        return 0

    return len(result)


# =========================================================
# 오늘 소통량 순위
# =========================================================

def get_today_ranking(group_id):
    today = datetime.now(KST).strftime("%Y-%m-%d")

    params = {
        "group_id": f"eq.{group_id}",
        "date_kst": f"eq.{today}",
        "select": "user_id,user_name",
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if not result:
        return []

    users = {}

    for row in result:
        user_id = row.get("user_id")
        user_name = row.get("user_name", "알 수 없음")

        if user_id not in users:
            users[user_id] = {
                "name": user_name,
                "count": 0
            }

        users[user_id]["count"] += 1

    ranking = list(users.values())

    ranking.sort(
        key=lambda x: x["count"],
        reverse=True
    )

    return ranking


# =========================================================
# 방 전체 통계
# =========================================================

def get_room_stats(group_id):
    params = {
        "group_id": f"eq.{group_id}",
        "select": "user_id,date_kst,hour_kst",
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if not result:
        return {
            "total": 0,
            "users": 0,
            "best_day": "없음"
        }

    total = len(result)

    users = set()
    day_count = {}

    for row in result:
        users.add(row.get("user_id"))

        day = row.get("date_kst")

        if day:
            day_count[day] = day_count.get(day, 0) + 1

    best_day = "없음"

    if day_count:
        best_day = max(
            day_count,
            key=day_count.get
        )

    return {
        "total": total,
        "users": len(users),
        "best_day": best_day
    }


# =========================================================
# 개인 통계
# =========================================================

def get_personal_stats(group_id, user_id):
    params = {
        "group_id": f"eq.{group_id}",
        "user_id": f"eq.{user_id}",
        "select": "date_kst,hour_kst",
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if not result:
        return {
            "total": 0,
            "rank": 0,
            "best_hour": "없음"
        }

    total = len(result)

    hour_count = {}

    for row in result:
        hour = row.get("hour_kst")

        if hour is not None:
            hour_count[hour] = hour_count.get(hour, 0) + 1

    best_hour = "없음"

    if hour_count:
        best_hour_num = max(
            hour_count,
            key=hour_count.get
        )

        best_hour = f"{best_hour_num:02d}시"

    # 전체 사용자 수 계산
    all_params = {
        "group_id": f"eq.{group_id}",
        "select": "user_id",
    }

    all_result = supabase_request(
        "GET",
        "chat_messages",
        params=all_params
    )

    rank = 1

    if all_result:
        counts = {}

        for row in all_result:
            uid = row.get("user_id")

            if uid:
                counts[uid] = counts.get(uid, 0) + 1

        sorted_users = sorted(
            counts.items(),
            key=lambda x: x[1],
            reverse=True
        )

        for index, (uid, count) in enumerate(
            sorted_users,
            start=1
        ):
            if uid == user_id:
                rank = index
                break

    return {
        "total": total,
        "rank": rank,
        "best_hour": best_hour
    }


# =========================================================
# 이름 생성
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
    "9": "⁹",
}


def to_superscript(number):
    number = str(number)

    return "".join(
        SUPERSCRIPT_MAP.get(char, char)
        for char in number
    )


def make_nickname(text):
    parts = text.strip().split()

    # 이름 성별 나이 타입
    if len(parts) != 4:
        return None

    name = parts[0]
    gender = parts[1]
    age = parts[2]
    type_word = parts[3]

    if not age.isdigit():
        return None

    if len(age) == 1:
        age = "0" + age

    if len(age) != 2:
        return None

    type_map = {
        "돔": "𝒅",
        "섭": "𝒔",
        "스위치": "𝒔/𝒅",
        "바닐라": "𝒗",
    }

    if type_word not in type_map:
        return None

    type_symbol = type_map[type_word]

    gender_map = {
        "여자": "📡",
        "남자": "🔭",
    }

    if gender not in gender_map:
        return None

    gender_emoji = gender_map[gender]

    return (
        f"{name}"
        f"{type_symbol}"
        f"{to_superscript(age)}"
        f"{gender_emoji}"
    )


# =========================================================
# 최근 채팅 가져오기
# =========================================================

def get_recent_messages(group_id):
    params = {
        "group_id": f"eq.{group_id}",
        "select": "user_name,message,created_at",
        "order": "created_at.desc",
        "limit": str(RECENT_MESSAGE_LIMIT),
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if not result:
        return []

    result.reverse()

    messages = []

    for row in result:
        name = row.get("user_name", "누군가")
        message = row.get("message", "")

        messages.append(
            f"{name}: {message}"
        )

    return messages


# =========================================================
# Gemini AI
# =========================================================

def ask_gemini(prompt):
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY가 없습니다.")
        return None

    for model in GEMINI_MODELS:

        print(f"Gemini 모델 시도: {model}")

        url = (
            "https://generativelanguage.googleapis.com/"
            f"v1beta/models/{model}:generateContent"
            f"?key={GEMINI_API_KEY}"
        )

        payload = {
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
                "temperature": 0.9,
                "maxOutputTokens": 300,
            }
        }

        # 같은 모델에서 최대 3번 재시도
        for attempt in range(3):

            try:
                response = requests.post(
                    url,
                    json=payload,
                    timeout=30
                )

                # 성공
                if response.status_code == 200:

                    data = response.json()

                    candidates = data.get(
                        "candidates",
                        []
                    )

                    if not candidates:
                        print(
                            f"{model}: candidates 없음"
                        )
                        break

                    content = candidates[0].get(
                        "content",
                        {}
                    )

                    parts = content.get(
                        "parts",
                        []
                    )

                    if not parts:
                        print(
                            f"{model}: parts 없음"
                        )
                        break

                    answer = parts[0].get(
                        "text",
                        ""
                    ).strip()

                    if answer:
                        print(
                            f"Gemini 성공: {model}"
                        )
                        return answer

                    break

                # 일시적인 오류
                if response.status_code in [
                    408,
                    429,
                    500,
                    502,
                    503,
                    504
                ]:

                    print(
                        f"Gemini 일시적 오류 "
                        f"{response.status_code} "
                        f"({model}) "
                        f"({attempt + 1}/3)"
                    )

                    if attempt < 2:
                        wait_time = [1, 3, 7][attempt]
                        time.sleep(wait_time)
                        continue

                    # 같은 모델 3회 실패
                    print(
                        f"{model} 3회 실패 → "
                        f"다음 모델로 이동"
                    )

                    break

                # 그 외 오류
                print(
                    f"Gemini 오류 "
                    f"{response.status_code}: "
                    f"{response.text}"
                )

                break

            except requests.exceptions.Timeout:

                print(
                    f"Gemini Timeout "
                    f"({model}) "
                    f"({attempt + 1}/3)"
                )

                if attempt < 2:
                    wait_time = [1, 3, 7][attempt]
                    time.sleep(wait_time)
                    continue

                break

            except Exception as e:

                print(
                    f"Gemini 예외 "
                    f"({model}): {e}"
                )

                break

    print("모든 Gemini 모델 실패")

    return None


# =========================================================
# ! 질문
# =========================================================

def answer_exclamation_question(question):
    if not question.strip():
        return "뭘 물어보는 거야? 😶"

    prompt = f"""
너는 LINE 단체채팅방에서 친구처럼 대화하는 AI야.

사용자가 보낸 질문:
{question}

규칙:
- 한국어로 답해.
- 너무 길게 설명하지 마.
- 자연스럽고 편하게 답해.
- 친구가 물어본 것처럼 답해.
- 필요한 경우에만 자세히 설명해.
- 모르는 것은 모른다고 말해.
- 사실을 지어내지 마.
- 정치, 종교, 성적인 내용, 개인정보, 싸움이나 위험한 행동 등 민감한 주제는 중립적으로 답해.
"""

    return ask_gemini(prompt)


# =========================================================
# 마지막 AI 개입 시간
# =========================================================

def get_last_ai_reply(group_id):
    params = {
        "group_id": f"eq.{group_id}",
        "select": "last_ai_reply",
        "limit": "1",
    }

    result = supabase_request(
        "GET",
        "bot_state",
        params=params
    )

    if not result:
        return None

    value = result[0].get("last_ai_reply")

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

    except Exception:
        return None


def update_last_ai_reply(group_id):
    now = datetime.now(timezone.utc).isoformat()

    # 먼저 존재 여부 확인
    params = {
        "group_id": f"eq.{group_id}",
        "select": "group_id",
        "limit": "1",
    }

    result = supabase_request(
        "GET",
        "bot_state",
        params=params
    )

    data = {
        "group_id": group_id,
        "last_ai_reply": now
    }

    if result:
        supabase_request(
            "PATCH",
            "bot_state",
            params={
                "group_id": f"eq.{group_id}"
            },
            data=data
        )

    else:
        supabase_request(
            "POST",
            "bot_state",
            data=data
        )


# =========================================================
# AI 랜덤 개입
# =========================================================

def maybe_ai_intervene(group_id):
    # 확률 체크
    if random.random() > AI_TRIGGER_CHANCE:
        return None

    # 마지막 개입 시간 확인
    last_reply = get_last_ai_reply(group_id)

    if last_reply:
        now = datetime.now(timezone.utc)

        elapsed = (
            now - last_reply
        ).total_seconds()

        if elapsed < AI_COOLDOWN_SECONDS:
            return None

    recent_messages = get_recent_messages(group_id)

    if not recent_messages:
        return None

    chat_text = "\n".join(recent_messages)

    prompt = f"""
너는 LINE 단체채팅방에 들어와 있는 친구 같은 AI야.

최근 대화:
{chat_text}

지금 이 대화에 네가 자연스럽게 한마디 끼어들 만한 상황인지 판단해.

중요:
- 억지로 끼어들지 마.
- 특별히 할 말이 없으면 반드시 NO라고 답해.
- 자연스럽게 반응할 이유가 있을 때만 YES로 답해.
- 너무 진지하거나 민감한 주제에는 끼어들지 마.
- 정치, 종교, 성적인 이야기, 개인정보, 싸움 등에는 개입하지 마.
- 친구 단톡방처럼 짧고 자연스럽게 말해.
- 너무 AI 같은 말투를 사용하지 마.
- 같은 표현을 반복하지 마.

형식은 반드시 아래 둘 중 하나:

NO

또는

YES|짧은 답변

답변은 최대 1~2문장으로 해.
"""

    answer = ask_gemini(prompt)

    if not answer:
        return None

    answer = answer.strip()

    if answer.upper() == "NO":
        return None

    if answer.upper().startswith("YES|"):
        reply = answer[4:].strip()

        if not reply:
            return None

        update_last_ai_reply(group_id)

        return reply

    return None


# =========================================================
# 오늘 소통량 초기화
# =========================================================

def reset_today(group_id):
    today = datetime.now(KST).strftime("%Y-%m-%d")

    params = {
        "group_id": f"eq.{group_id}",
        "date_kst": f"eq.{today}",
    }

    return supabase_request(
        "DELETE",
        "chat_messages",
        params=params
    )


# =========================================================
# 웹훅
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    body = request.get_data(as_text=True)

    signature = request.headers.get(
        "X-Line-Signature",
        ""
    )

    # LINE 서명 검증
    hash_value = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256
    ).digest()

    expected_signature = base64.b64encode(
        hash_value
    ).decode("utf-8")

    if not hmac.compare_digest(
        signature,
        expected_signature
    ):
        print("LINE 서명 검증 실패")
        abort(400)

    try:
        data = json.loads(body)

    except Exception:
        abort(400)

    events = data.get("events", [])

    for event in events:

        # 메시지 이벤트가 아니면 무시
        if event.get("type") != "message":
            continue

        message = event.get("message", {})

        # 텍스트만 처리
        if message.get("type") != "text":
            continue

        text = message.get("text", "").strip()

        if not text:
            continue

        source = event.get("source", {})

        # 단체방만 처리
        if source.get("type") != "group":
            continue

        group_id = source.get("groupId")
        user_id = source.get("userId")

        reply_token = event.get("replyToken")

        # 사용자 이름
        user_name = get_profile_name(user_id)


        # =================================================
        # 이름 생성 기능
        # =================================================

        nickname = make_nickname(text)

        if nickname:

            reply_message(
                reply_token,
                nickname
            )

            continue


        # =================================================
        # 오늘 소통량 초기화
        # =================================================

        normalized = text.replace(" ", "")

        if normalized in [
            "단라소통량초기화",
            "소통량초기화"
        ]:

            reset_today(group_id)

            reply_message(
                reply_token,
                "오늘 소통량을 초기화했어."
            )

            continue


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
                f"{user_name}님의 오늘 소통량은 "
                f"{count}개야."
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

            result = "🏆 단라 소통량 순위\n\n"

            for index, user in enumerate(
                ranking,
                start=1
            ):

                result += (
                    f"{index}위 "
                    f"{user['name']} "
                    f"{user['count']}개\n"
                )

            reply_message(
                reply_token,
                result
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
                f"총 메시지: {stats['total']}개\n"
                f"참여 인원: {stats['users']}명\n"
                f"가장 활발했던 날: {stats['best_day']}"
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

            result = (
                f"📊 {user_name}님의 통계\n\n"
                f"총 소통량: {stats['total']}개\n"
                f"방 내 순위: {stats['rank']}위\n"
                f"가장 활발한 시간: "
                f"{stats['best_hour']}"
            )

            reply_message(
                reply_token,
                result
            )

            continue


        # =================================================
        # ! 로 시작하면 무조건 AI 질문
        # =================================================

        if text.startswith("!"):

            question = text[1:].strip()

            answer = answer_exclamation_question(
                question
            )

            if answer:

                reply_message(
                    reply_token,
                    answer
                )

            else:

                reply_message(
                    reply_token,
                    "잠깐, 지금은 답변을 못하겠어."
                )

            # AI 질문은 소통량에 포함하지 않음
            continue


        # =================================================
        # 일반 메시지
        # =================================================

        # ㅋㅋㅋㅋ / ㅎㅎ / ㅠㅠ 같은 메시지는 저장하지 않음
        if not is_laugh_only(text):

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
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
