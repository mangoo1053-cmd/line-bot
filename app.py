from flask import Flask, request, abort
import requests
import hashlib
import hmac
import base64
import json
import os
import re
import random
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

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")


# =========================================================
# 설정
# =========================================================

KST = ZoneInfo("Asia/Seoul")

# 일반적인 랜덤 AI 개입
AI_TRIGGER_CHANCE = 0.03

# 랜덤 AI 개입 최소 간격
AI_COOLDOWN_SECONDS = 3 * 60 * 60

# AI가 볼 최근 메시지 수
RECENT_MESSAGE_LIMIT = 12


# =========================================================
# 기본
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "LINE Bot is running", 200


# =========================================================
# Supabase 공통 요청
# =========================================================

def supabase_request(method, table, params=None, json_data=None):
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
        headers["Prefer"] = "return=representation"

    if method.upper() == "PATCH":
        headers["Prefer"] = "return=representation"

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_data,
            timeout=10
        )

        if not response.ok:
            print("Supabase 오류:")
            print(response.status_code)
            print(response.text)
            return None

        if not response.text:
            return []

        return response.json()

    except Exception as e:
        print("Supabase 요청 오류:", e)
        return None


# =========================================================
# LINE API
# =========================================================

def line_api(endpoint, method="GET", data=None):
    url = f"https://api.line.me/v2/bot/{endpoint}"

    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        if method == "POST":
            response = requests.post(
                url,
                headers=headers,
                json=data,
                timeout=10
            )
        else:
            response = requests.get(
                url,
                headers=headers,
                timeout=10
            )

        if response.ok:
            if response.text:
                return response.json()
            return {}

        print("LINE API 오류:")
        print(response.status_code)
        print(response.text)

    except Exception as e:
        print("LINE API 요청 오류:", e)

    return None


def reply_message(reply_token, text):
    if not text:
        return

    data = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": str(text)[:5000]
            }
        ]
    }

    line_api("message/reply", "POST", data)


# =========================================================
# LINE 사용자 이름
# =========================================================

def get_profile_name(user_id):
    result = line_api(f"profile/{user_id}")

    if result and result.get("displayName"):
        return result["displayName"]

    return "알 수 없음"


# =========================================================
# 웃음만 있는 메시지 제외
# =========================================================

def is_laugh_only(text):
    if not text:
        return False

    text = text.strip()

    return bool(re.fullmatch(r"[ㅋㅎㅠㅜ]+", text))


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
        "hour_kst": now.hour
    }

    result = supabase_request(
        "POST",
        "chat_messages",
        json_data=data
    )

    if result is None:
        print("메시지 저장 실패")
        return False

    return True


# =========================================================
# 오늘 내 소통량
# =========================================================

def get_today_count(group_id, user_id):
    today = datetime.now(KST).strftime("%Y-%m-%d")

    params = {
        "select": "id",
        "group_id": f"eq.{group_id}",
        "user_id": f"eq.{user_id}",
        "date_kst": f"eq.{today}",
        "limit": "10000"
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
        "select": "user_id,user_name",
        "group_id": f"eq.{group_id}",
        "date_kst": f"eq.{today}",
        "limit": "10000"
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if result is None:
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

    ranking = sorted(
        users.items(),
        key=lambda x: x[1]["count"],
        reverse=True
    )

    return ranking


# =========================================================
# 방 통계
# =========================================================

def get_room_stats(group_id):
    params = {
        "select": "user_id,date_kst",
        "group_id": f"eq.{group_id}",
        "limit": "10000"
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if result is None:
        return 0, 0, "없음"

    total_messages = len(result)

    users = set()

    daily = {}

    for row in result:
        user_id = row.get("user_id")
        date = row.get("date_kst")

        if user_id:
            users.add(user_id)

        if date:
            daily[date] = daily.get(date, 0) + 1

    if daily:
        busiest_day = max(
            daily.items(),
            key=lambda x: x[1]
        )

        busiest_day_text = (
            f"{busiest_day[0]} "
            f"({busiest_day[1]}개)"
        )
    else:
        busiest_day_text = "없음"

    return (
        total_messages,
        len(users),
        busiest_day_text
    )


# =========================================================
# 내 통계
# =========================================================

def get_personal_stats(group_id, user_id):
    params = {
        "select": "user_id,hour_kst",
        "group_id": f"eq.{group_id}",
        "limit": "10000"
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if result is None:
        return 0, 0, "없음"

    my_messages = [
        row for row in result
        if row.get("user_id") == user_id
    ]

    total_count = len(my_messages)

    # 전체 사용자별 소통량
    user_counts = {}

    for row in result:
        uid = row.get("user_id")

        if uid:
            user_counts[uid] = user_counts.get(uid, 0) + 1

    sorted_users = sorted(
        user_counts.items(),
        key=lambda x: x[1],
        reverse=True
    )

    my_rank = 0

    for index, (uid, count) in enumerate(sorted_users, start=1):
        if uid == user_id:
            my_rank = index
            break

    # 가장 활발한 시간
    hours = {}

    for row in my_messages:
        hour = row.get("hour_kst")

        if hour is not None:
            hours[hour] = hours.get(hour, 0) + 1

    if hours:
        active_hour = max(
            hours.items(),
            key=lambda x: x[1]
        )

        active_hour_text = (
            f"{active_hour[0]:02d}시"
            f" ({active_hour[1]}개)"
        )
    else:
        active_hour_text = "없음"

    return total_count, my_rank, active_hour_text


# =========================================================
# 이름 생성
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
    "9": "⁹"
}


def to_superscript(text):
    return "".join(
        SUPERSCRIPT.get(char, char)
        for char in text
    )


def make_nickname(text):
    parts = text.strip().split()

    # 이름 성별 나이 돔/섭/스위치/바닐라
    if len(parts) != 4:
        return None

    name = parts[0]
    gender = parts[1]
    age = parts[2]
    role = parts[3]

    # 나이는 정확히 두 자리
    if not re.fullmatch(r"\d{2}", age):
        return None

    role_map = {
        "돔": "𝒅",
        "섭": "𝒔",
        "스위치": "𝒔/𝒅",
        "바닐라": "𝒗"
    }

    gender_map = {
        "여자": "📡",
        "남자": "🔭"
    }

    if role not in role_map:
        return None

    if gender not in gender_map:
        return None

    role_text = role_map[role]
    gender_text = gender_map[gender]

    return (
        f"{name}"
        f"{role_text}"
        f"{to_superscript(age)}"
        f"{gender_text}"
    )


# =========================================================
# 최근 메시지
# =========================================================

def get_recent_messages(group_id):
    params = {
        "select": "user_name,message,date_kst,hour_kst",
        "group_id": f"eq.{group_id}",
        "order": "id.desc",
        "limit": str(RECENT_MESSAGE_LIMIT)
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
        name = row.get("user_name", "알 수 없음")
        message = row.get("message", "")

        messages.append(
            f"{name}: {message}"
        )

    return messages


# =========================================================
# OpenAI 기본 요청
# =========================================================

def ask_openai(instructions, user_input):
    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY가 없습니다.")
        return None

    url = "https://api.openai.com/v1/responses"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "gpt-6-luna",
        "instructions": instructions,
        "input": user_input
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=data,
            timeout=25
        )

        if not response.ok:
            print("OpenAI 오류:")
            print(response.status_code)
            print(response.text)
            return None

        result = response.json()

        # Responses API의 output_text가 있으면 사용
        if result.get("output_text"):
            return result["output_text"].strip()

        # 혹시 output_text가 없는 경우 직접 추출
        output = result.get("output", [])

        texts = []

        for item in output:
            if item.get("type") != "message":
                continue

            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text")

                    if text:
                        texts.append(text)

        if texts:
            return "\n".join(texts).strip()

    except Exception as e:
        print("OpenAI 요청 오류:", e)

    return None


# =========================================================
# !로 시작하는 모든 질문
# =========================================================

def answer_exclamation_question(question):
    instructions = """
너는 한국 친구들이 있는 단톡방에서 대화하는 자연스러운 AI 봇이다.

사용자가 ! 뒤에 적은 내용을 질문 또는 말로 받아들이고 답변한다.

중요한 규칙:
- 사용자가 !로 시작한 문장을 그대로 질문으로 이해한다.
- 친한 친구처럼 자연스럽게 답한다.
- 너무 딱딱하거나 AI 같은 말투를 사용하지 않는다.
- 질문에 필요한 만큼만 답한다.
- 간단한 질문은 짧게 답한다.
- 설명이 필요한 질문은 이해하기 쉽게 설명한다.
- 한국어로 답한다.
- 상황에 따라 반말을 사용해도 된다.
- 억지로 ㅋㅋ를 붙이지 않는다.
- 매번 똑같은 문장으로 답하지 않는다.
- 질문에 답할 수 없으면 솔직하게 말한다.
- 위험하거나 불법적인 요청 등에는 안전한 범위에서 답한다.
"""

    return ask_openai(
        instructions,
        question
    )


# =========================================================
# 랜덤 AI 개입용 상태
# =========================================================

def get_last_ai_reply(group_id):
    params = {
        "select": "last_ai_reply",
        "group_id": f"eq.{group_id}",
        "limit": "1"
    }

    result = supabase_request(
        "GET",
        "bot_state",
        params=params
    )

    if not result:
        return None

    return result[0].get("last_ai_reply")


def update_last_ai_reply(group_id):
    now = datetime.now(timezone.utc).isoformat()

    data = {
        "group_id": group_id,
        "last_ai_reply": now
    }

    url = f"{SUPABASE_URL}/rest/v1/bot_state"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal"
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=data,
            timeout=10
        )

        if not response.ok:
            print("bot_state 저장 오류:")
            print(response.status_code)
            print(response.text)

    except Exception as e:
        print("bot_state 저장 오류:", e)


# =========================================================
# 랜덤 AI 개입
# =========================================================

def maybe_ai_intervene(group_id, reply_token):
    # 3% 확률
    if random.random() > AI_TRIGGER_CHANCE:
        return

    # 마지막 AI 개입 시간 확인
    last_reply = get_last_ai_reply(group_id)

    if last_reply:
        try:
            last_time = datetime.fromisoformat(
                last_reply.replace("Z", "+00:00")
            )

            now = datetime.now(timezone.utc)

            elapsed = (
                now - last_time
            ).total_seconds()

            if elapsed < AI_COOLDOWN_SECONDS:
                return

        except Exception:
            pass

    recent = get_recent_messages(group_id)

    if not recent:
        return

    context = "\n".join(recent)

    instructions = """
너는 한국 단톡방에서 자연스럽게 대화하는 AI 봇이다.

아래 단톡방 대화를 보고 지금 네가 한마디 끼어드는 것이
자연스러운 상황인지 판단한다.

중요:
- 평범한 대화에는 억지로 끼어들지 않는다.
- 정말 한마디 하고 싶을 만한 상황일 때만 답한다.
- 특정 키워드만 보고 판단하지 말고 전체 대화 흐름을 본다.
- 답변한다면 짧고 자연스럽게 한다.
- AI가 일부러 끼어드는 느낌을 내지 않는다.
- 같은 표현을 반복하지 않는다.
- 민감한 주제, 정치, 종교, 성적인 주제, 개인정보, 심각한 싸움에는 끼어들지 않는다.

반드시 아래 형식 중 하나로 출력한다.

답할 필요가 없으면:
NO

답할 필요가 있으면:
YES|답변내용
"""

    result = ask_openai(
        instructions,
        context
    )

    if not result:
        return

    result = result.strip()

    if not result.startswith("YES|"):
        return

    reply = result[4:].strip()

    if not reply:
        return

    update_last_ai_reply(group_id)

    reply_message(
        reply_token,
        reply
    )


# =========================================================
# 오늘 소통량 초기화
# =========================================================

def reset_today(group_id):
    today = datetime.now(KST).strftime("%Y-%m-%d")

    params = {
        "group_id": f"eq.{group_id}",
        "date_kst": f"eq.{today}"
    }

    url = f"{SUPABASE_URL}/rest/v1/chat_messages"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }

    try:
        response = requests.delete(
            url,
            headers=headers,
            params=params,
            timeout=10
        )

        if response.ok:
            return True

        print("초기화 오류:")
        print(response.status_code)
        print(response.text)

    except Exception as e:
        print("초기화 오류:", e)

    return False


# =========================================================
# WEBHOOK
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():

    # -----------------------------------------------------
    # LINE 서명 확인
    # -----------------------------------------------------

    body = request.get_data(as_text=True)

    signature = request.headers.get(
        "X-Line-Signature"
    )

    if not signature:
        abort(400)

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
        abort(400)

    # -----------------------------------------------------
    # JSON
    # -----------------------------------------------------

    try:
        data = json.loads(body)
    except Exception:
        abort(400)

    events = data.get("events", [])

    for event in events:

        if event.get("type") != "message":
            continue

        message = event.get("message", {})

        # 텍스트만 처리
        if message.get("type") != "text":
            continue

        text = message.get("text", "").strip()

        if not text:
            continue

        reply_token = event.get("replyToken")

        source = event.get("source", {})

        # -------------------------------------------------
        # 단톡방만
        # -------------------------------------------------

        if source.get("type") != "group":
            continue

        group_id = source.get("groupId")
        user_id = source.get("userId")

        if not group_id or not user_id:
            continue

        # -------------------------------------------------
        # 사용자 이름
        # -------------------------------------------------

        user_name = get_profile_name(user_id)

        # -------------------------------------------------
        # 웃음만 있는 메시지는 소통량에서 제외
        # -------------------------------------------------

        should_count = not is_laugh_only(text)

        # -------------------------------------------------
        # 이름 생성
        # -------------------------------------------------

        nickname = make_nickname(text)

        if nickname:
            reply_message(
                reply_token,
                nickname
            )
            continue

        # -------------------------------------------------
        # 오늘 소통량 초기화
        # -------------------------------------------------

        normalized = text.replace(" ", "")

        if normalized in [
            "단라소통량초기화"
        ]:
            success = reset_today(group_id)

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

        # -------------------------------------------------
        # 내 소통량
        # -------------------------------------------------

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
                f"{user_name}님의 오늘 소통량은 {count}개야."
            )

            continue

        # -------------------------------------------------
        # 단라 소통량 순위
        # -------------------------------------------------

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

            lines = [
                "🏆 오늘 단라 소통량 순위"
            ]

            for index, (uid, info) in enumerate(
                ranking,
                start=1
            ):
                lines.append(
                    f"{index}위 {info['name']} "
                    f"{info['count']}개"
                )

            reply_message(
                reply_token,
                "\n".join(lines)
            )

            continue

        # -------------------------------------------------
        # 방 통계
        # -------------------------------------------------

        if normalized == "방통계":
            total, people, busiest = get_room_stats(
                group_id
            )

            reply_message(
                reply_token,
                "📊 방 통계\n"
                f"총 메시지: {total}개\n"
                f"참여 인원: {people}명\n"
                f"가장 활발했던 날: {busiest}"
            )

            continue

        # -------------------------------------------------
        # 내 통계
        # -------------------------------------------------

        if normalized == "내통계":
            total, rank, active_hour = get_personal_stats(
                group_id,
                user_id
            )

            reply_message(
                reply_token,
                "📊 내 통계\n"
                f"총 소통량: {total}개\n"
                f"방 내 순위: {rank}위\n"
                f"가장 활발한 시간: {active_hour}"
            )

            continue

        # =================================================
        # ! 로 시작하는 AI 질문
        # =================================================

        if text.startswith("!"):
            question = text[1:].strip()

            if question:
                ai_reply = answer_exclamation_question(
                    question
                )

                if ai_reply:
                    reply_message(
                        reply_token,
                        ai_reply
                    )
                else:
                    reply_message(
                        reply_token,
                        "잠깐 오류났어 ㅋㅋ 다시 물어봐"
                    )

            continue

        # -------------------------------------------------
        # 일반 메시지 저장
        # -------------------------------------------------

        if should_count:
            save_message(
                group_id,
                user_id,
                user_name,
                text
            )

        # -------------------------------------------------
        # 랜덤 AI 개입
        # -------------------------------------------------

        if should_count:
            maybe_ai_intervene(
                group_id,
                reply_token
            )

    return "OK", 200


# =========================================================
# 실행
# =========================================================

if __name__ == "__main__":
    port = int(
        os.environ.get("PORT", 10000)
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
