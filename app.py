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

# 랜덤 AI 개입 확률
AI_TRIGGER_CHANCE = 0.03

# 랜덤 AI 개입 후 3시간 쿨타임
AI_COOLDOWN_SECONDS = 3 * 60 * 60

# AI가 참고할 최근 대화 수
RECENT_MESSAGE_LIMIT = 12


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
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Supabase 환경변수가 없습니다.")
        return None

    url = f"{SUPABASE_URL}/rest/v1/{table}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=data,
            timeout=15
        )

        if not response.ok:
            print("Supabase 오류:")
            print(response.status_code)
            print(response.text)
            return None

        if response.text:
            try:
                return response.json()
            except Exception:
                return response.text

        return None

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
        response = requests.request(
            method,
            url,
            headers=headers,
            json=data,
            timeout=15
        )

        if not response.ok:
            print("LINE API 오류:")
            print(response.status_code)
            print(response.text)

        return response

    except Exception as e:
        print("LINE API 요청 오류:", e)
        return None


def reply_message(reply_token, text):
    if not reply_token:
        return

    data = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": text[:5000]
            }
        ]
    }

    line_api(
        "message/reply",
        "POST",
        data
    )


# =========================================================
# LINE 프로필
# =========================================================

def get_profile_name(user_id):
    response = line_api(
        f"profile/{user_id}"
    )

    if response and response.ok:
        try:
            data = response.json()
            return data.get(
                "displayName",
                "알 수 없음"
            )
        except Exception:
            pass

    return "알 수 없음"


# =========================================================
# 소통량
# =========================================================

def is_laugh_only(text):
    if not text:
        return True

    text = text.strip()

    if not text:
        return True

    return re.fullmatch(
        r"[ㅋㅎㅠㅜ]+",
        text
    ) is not None


def save_message(
    group_id,
    user_id,
    user_name,
    message
):
    if is_laugh_only(message):
        return

    now = datetime.now(KST)

    data = {
        "group_id": group_id,
        "user_id": user_id,
        "user_name": user_name,
        "message": message,
        "date_kst": now.strftime("%Y-%m-%d"),
        "hour_kst": now.hour
    }

    supabase_request(
        "POST",
        "chat_messages",
        data=data
    )


def get_today_count(
    group_id,
    user_id
):
    today = datetime.now(KST).strftime(
        "%Y-%m-%d"
    )

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

    if isinstance(result, list):
        return len(result)

    return 0


def get_today_ranking(group_id):
    today = datetime.now(KST).strftime(
        "%Y-%m-%d"
    )

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

    if not isinstance(result, list):
        return []

    ranking = {}

    for row in result:
        user_id = row.get("user_id")
        user_name = row.get(
            "user_name",
            "알 수 없음"
        )

        if not user_id:
            continue

        if user_id not in ranking:
            ranking[user_id] = {
                "name": user_name,
                "count": 0
            }

        ranking[user_id]["count"] += 1

    ranking_list = list(
        ranking.values()
    )

    ranking_list.sort(
        key=lambda x: x["count"],
        reverse=True
    )

    return ranking_list


# =========================================================
# 방 통계
# =========================================================

def get_room_stats(group_id):
    params = {
        "select": "user_id,date_kst,hour_kst",
        "group_id": f"eq.{group_id}",
        "limit": "10000"
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if not isinstance(result, list):
        return None

    if not result:
        return None

    total_messages = len(result)

    users = set()
    daily = {}

    for row in result:
        user_id = row.get("user_id")
        date = row.get("date_kst")

        if user_id:
            users.add(user_id)

        if date:
            daily[date] = (
                daily.get(date, 0) + 1
            )

    most_active_day = None

    if daily:
        most_active_day = max(
            daily,
            key=daily.get
        )

    return {
        "total_messages": total_messages,
        "users": len(users),
        "most_active_day": most_active_day,
        "most_active_day_count":
            daily.get(
                most_active_day,
                0
            )
            if most_active_day
            else 0
    }


# =========================================================
# 개인 통계
# =========================================================

def get_personal_stats(
    group_id,
    user_id
):
    params = {
        "select": "user_id,user_name,hour_kst",
        "group_id": f"eq.{group_id}",
        "user_id": f"eq.{user_id}",
        "limit": "10000"
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if not isinstance(result, list):
        return None

    if not result:
        return None

    total = len(result)

    hour_counts = {}

    for row in result:
        hour = row.get("hour_kst")

        if hour is not None:
            hour_counts[hour] = (
                hour_counts.get(hour, 0) + 1
            )

    most_active_hour = None

    if hour_counts:
        most_active_hour = max(
            hour_counts,
            key=hour_counts.get
        )

    # 방 전체 사용자별 소통량
    all_params = {
        "select": "user_id",
        "group_id": f"eq.{group_id}",
        "limit": "10000"
    }

    all_result = supabase_request(
        "GET",
        "chat_messages",
        params=all_params
    )

    user_counts = {}

    if isinstance(all_result, list):
        for row in all_result:
            uid = row.get("user_id")

            if uid:
                user_counts[uid] = (
                    user_counts.get(uid, 0) + 1
                )

    sorted_users = sorted(
        user_counts.items(),
        key=lambda x: x[1],
        reverse=True
    )

    rank = None

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
        "most_active_hour":
            most_active_hour,
        "most_active_hour_count":
            hour_counts.get(
                most_active_hour,
                0
            )
            if most_active_hour is not None
            else 0
    }


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


def to_superscript(number):
    return "".join(
        SUPERSCRIPT.get(
            char,
            char
        )
        for char in str(number)
    )


def make_nickname(text):
    parts = text.strip().split()

    if len(parts) != 4:
        return None

    name = parts[0]
    gender = parts[1]
    age = parts[2]
    role = parts[3]

    if gender not in [
        "남자",
        "여자"
    ]:
        return None

    if not re.fullmatch(
        r"\d{1,2}",
        age
    ):
        return None

    age = age.zfill(2)

    role_map = {
        "돔": "𝒅",
        "섭": "𝒔",
        "스위치": "𝒔/𝒅",
        "바닐라": "𝒗"
    }

    if role not in role_map:
        return None

    role_text = role_map[role]

    gender_emoji = (
        "📡"
        if gender == "여자"
        else "🔭"
    )

    return (
        f"{name}"
        f"{role_text}"
        f"{to_superscript(age)}"
        f"{gender_emoji}"
    )


# =========================================================
# 최근 대화
# =========================================================

def get_recent_messages(group_id):
    params = {
        "select": "user_name,message",
        "group_id": f"eq.{group_id}",
        "order": "created_at.desc",
        "limit": str(
            RECENT_MESSAGE_LIMIT
        )
    }

    result = supabase_request(
        "GET",
        "chat_messages",
        params=params
    )

    if not isinstance(result, list):
        return []

    result.reverse()

    messages = []

    for row in result:
        name = row.get(
            "user_name",
            "알 수 없음"
        )

        message = row.get(
            "message",
            ""
        )

        if message:
            messages.append(
                f"{name}: {message}"
            )

    return messages


# =========================================================
# Gemini
# =========================================================

def ask_gemini(
    prompt,
    system_instruction=None
):
    if not GEMINI_API_KEY:
        print(
            "GEMINI_API_KEY가 없습니다."
        )
        return None

    # 현재 사용 모델
    model = "gemini-3.8-flash"

    url = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model}:generateContent"
    )

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": prompt
                    }
                ]
            }
        ]
    }

    if system_instruction:
        payload["system_instruction"] = {
            "parts": [
                {
                    "text": system_instruction
                }
            ]
        }

    # =====================================================
    # Gemini 일시적 오류 재시도
    # =====================================================

    retry_delays = [
        1,
        3,
        7
    ]

    for attempt in range(3):

        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=30
            )

            # ---------------------------------------------
            # 성공
            # ---------------------------------------------

            if response.ok:

                data = response.json()

                candidates = data.get(
                    "candidates",
                    []
                )

                if not candidates:
                    print(
                        "Gemini 응답에 candidates가 없습니다."
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

            # ---------------------------------------------
            # 일시적 오류
            # ---------------------------------------------

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
                    f"({attempt + 1}/3)"
                )

                print(
                    response.text
                )

                if attempt < 2:
                    time.sleep(
                        retry_delays[attempt]
                    )
                    continue

            # ---------------------------------------------
            # 그 외 오류
            # ---------------------------------------------

            print("Gemini 오류:")
            print(response.status_code)
            print(response.text)

            return None

        except requests.exceptions.Timeout:

            print(
                f"Gemini 시간 초과 "
                f"({attempt + 1}/3)"
            )

            if attempt < 2:
                time.sleep(
                    retry_delays[attempt]
                )
                continue

            return None

        except Exception as e:

            print(
                "Gemini 요청 오류:",
                e
            )

            return None

    return None


# =========================================================
# ! 질문
# =========================================================

def answer_exclamation_question(text):

    question = text[1:].strip()

    if not question:
        return "뭘 물어보는 거야?"

    system_instruction = """
너는 LINE 단체채팅방에서 사용하는 AI야.

사용자가 ! 뒤에 질문이나 말을 입력하면
그 내용에 자연스럽게 답해줘.

규칙:
- 한국어로 답해.
- 너무 길게 답하지 마.
- 친구와 대화하듯 자연스럽게 답해.
- 질문에는 정확하게 답해.
- 모르는 것은 아는 척하지 말고 모른다고 말해.
- 필요하면 간단한 설명을 붙여.
- 쓸데없이 AI라고 소개하지 마.
- 너무 딱딱하게 말하지 마.
"""

    return ask_gemini(
        question,
        system_instruction
    )


# =========================================================
# 랜덤 AI 개입
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

    if isinstance(result, list):
        if result:
            return result[0].get(
                "last_ai_reply"
            )

    return None


def update_last_ai_reply(group_id):

    now = datetime.now(
        timezone.utc
    ).isoformat()

    data = {
        "group_id": group_id,
        "last_ai_reply": now
    }

    existing = supabase_request(
        "GET",
        "bot_state",
        params={
            "select": "group_id",
            "group_id": f"eq.{group_id}",
            "limit": "1"
        }
    )

    if (
        isinstance(existing, list)
        and existing
    ):

        supabase_request(
            "PATCH",
            "bot_state",
            params={
                "group_id": f"eq.{group_id}"
            },
            data={
                "last_ai_reply": now
            }
        )

    else:

        supabase_request(
            "POST",
            "bot_state",
            data=data
        )


def maybe_ai_intervene(group_id):

    # 3% 확률
    if random.random() > AI_TRIGGER_CHANCE:
        return None

    # 마지막 AI 답변 확인
    last_reply = get_last_ai_reply(
        group_id
    )

    if last_reply:

        try:
            last_dt = datetime.fromisoformat(
                last_reply.replace(
                    "Z",
                    "+00:00"
                )
            )

            now = datetime.now(
                timezone.utc
            )

            elapsed = (
                now - last_dt
            ).total_seconds()

            if elapsed < AI_COOLDOWN_SECONDS:
                return None

        except Exception:
            pass

    recent = get_recent_messages(
        group_id
    )

    if not recent:
        return None

    conversation = "\n".join(
        recent
    )

    system_instruction = """
너는 한국인 친구들이 있는
LINE 단체채팅방에 자연스럽게 끼어드는 AI야.

최근 대화를 보고
지금 AI가 한마디 하는 것이
자연스러운지 판단해.

반드시 아래 형식 중 하나로만 답해.

AI가 끼어들 필요가 없으면:
NO

AI가 끼어드는 게 자연스러우면:
YES|짧은 답변

규칙:
- 정말 자연스러운 경우에만 YES.
- 억지로 대화에 끼어들지 마.
- 답변은 짧고 친구처럼.
- 너무 설명하지 마.
- 정치, 종교, 성적인 내용,
  개인정보, 심각한 갈등이나 싸움에는 개입하지 마.
- 질문에 답하거나 가벼운 농담을 하는 정도로 해.
"""

    prompt = f"""
최근 단체채팅 대화:

{conversation}

지금 AI가 한마디 하는 것이
자연스러운지 판단해.
"""

    result = ask_gemini(
        prompt,
        system_instruction
    )

    if not result:
        return None

    result = result.strip()

    if result == "NO":
        return None

    if result.startswith("YES|"):

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

    today = datetime.now(
        KST
    ).strftime("%Y-%m-%d")

    result = supabase_request(
        "DELETE",
        "chat_messages",
        params={
            "group_id": f"eq.{group_id}",
            "date_kst": f"eq.{today}"
        }
    )

    return result is not None


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
        "X-Line-Signature"
    )

    if not signature:
        abort(400)

    try:

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
            ).decode("utf-8")
        )

        if not hmac.compare_digest(
            expected_signature,
            signature
        ):
            abort(400)

    except Exception:
        abort(400)

    try:
        data = json.loads(body)

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

        # 텍스트만 처리
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

        # 그룹 채팅만
        if source.get("type") != "group":
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

        # 사용자 이름
        user_name = get_profile_name(
            user_id
        )

        # =================================================
        # 이름 생성
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
        # 공백 제거한 명령어
        # =================================================

        normalized = text.replace(
            " ",
            ""
        )

        # =================================================
        # 소통량 초기화
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
                f"{user_name}님의 오늘 소통량은 {count}개야."
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
                    "오늘 소통량 데이터가 없어."
                )

                continue

            lines = [
                "🏆 오늘 단라 소통량 순위"
            ]

            for i, person in enumerate(
                ranking,
                start=1
            ):

                lines.append(
                    f"{i}위 "
                    f"{person['name']} - "
                    f"{person['count']}개"
                )

            reply_message(
                reply_token,
                "\n".join(lines)
            )

            continue

        # =================================================
        # 방 통계
        # =================================================

        if normalized == "방통계":

            stats = get_room_stats(
                group_id
            )

            if not stats:

                reply_message(
                    reply_token,
                    "아직 통계 데이터가 없어."
                )

                continue

            result = (
                "📊 방 통계\n\n"
                f"총 메시지: "
                f"{stats['total_messages']}개\n"
                f"참여 인원: "
                f"{stats['users']}명\n"
                f"가장 활발했던 날: "
                f"{stats['most_active_day']} "
                f"({stats['most_active_day_count']}개)"
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

            if not stats:

                reply_message(
                    reply_token,
                    "아직 통계 데이터가 없어."
                )

                continue

            hour = stats[
                "most_active_hour"
            ]

            if hour is not None:

                hour_text = (
                    f"{hour:02d}시 "
                    f"({stats['most_active_hour_count']}개)"
                )

            else:

                hour_text = "없음"

            if stats["rank"]:

                rank_text = (
                    f"{stats['rank']}위"
                )

            else:

                rank_text = "없음"

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
        # ! 로 시작하면 무조건 Gemini
        # =================================================

        if text.startswith("!"):

            answer = answer_exclamation_question(
                text
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

            continue

        # =================================================
        # 일반 메시지 저장
        # =================================================

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

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                10000
            )
        )
    )
