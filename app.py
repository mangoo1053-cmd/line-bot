from flask import Flask, request, abort
import requests
import hashlib
import hmac
import base64
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

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")


# =========================================================
# 설정
# =========================================================

KST = ZoneInfo("Asia/Seoul")

# AI 개입 확률 3%
AI_TRIGGER_CHANCE = 0.03

# AI 개입 후 3시간 쿨다운
AI_COOLDOWN_SECONDS = 3 * 60 * 60

# AI가 참고할 최근 메시지 수
RECENT_MESSAGE_LIMIT = 12


# =========================================================
# 환경변수 확인
# =========================================================

if not CHANNEL_ACCESS_TOKEN:
    print("WARNING: CHANNEL_ACCESS_TOKEN이 없습니다.")

if not CHANNEL_SECRET:
    print("WARNING: CHANNEL_SECRET이 없습니다.")

if not SUPABASE_URL:
    print("WARNING: SUPABASE_URL이 없습니다.")

if not SUPABASE_KEY:
    print("WARNING: SUPABASE_KEY가 없습니다.")

if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY가 없습니다.")


# =========================================================
# 시간
# =========================================================

def get_now():
    return datetime.now(KST)


def get_today():
    return get_now().date().isoformat()


# =========================================================
# Supabase
# =========================================================

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }


def supabase_request(method, table, params=None, data=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Supabase 환경변수가 없습니다.")
        return None

    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=supabase_headers(),
            params=params,
            json=data,
            timeout=10
        )

        if not response.ok:
            print("Supabase 오류:")
            print(response.status_code)
            print(response.text)
            return None

        if response.text:
            return response.json()

        return []

    except Exception as e:
        print("Supabase 연결 오류:", e)
        return None


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

        if not response.ok:
            print("LINE API 오류:")
            print(response.status_code)
            print(response.text)

        return response

    except Exception as e:
        print("LINE API 연결 오류:", e)
        return None


def reply_message(reply_token, text):
    payload = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }

    return line_api("message/reply", payload)


def push_message(group_id, text):
    payload = {
        "to": group_id,
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }

    return line_api("message/push", payload)


# =========================================================
# LINE 사용자 이름
# =========================================================

def get_profile_name(group_id, user_id):
    url = (
        f"https://api.line.me/v2/bot/group/"
        f"{group_id}/member/{user_id}"
    )

    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"
    }

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=10
        )

        if response.ok:
            data = response.json()
            return data.get("displayName", "사용자")

        print(
            "프로필 이름 가져오기 실패:",
            response.status_code,
            response.text
        )

    except Exception as e:
        print("프로필 이름 오류:", e)

    return "사용자"


# =========================================================
# 웃음/울음만 있는 메시지 제외
# =========================================================

def is_laugh_only(text):
    if not text:
        return True

    text = text.strip()

    if not text:
        return True

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
    now = get_now()

    data = {
        "group_id": group_id,
        "user_id": user_id,
        "user_name": user_name,
        "message": message,
        "date_kst": now.date().isoformat(),
        "hour_kst": now.hour,
        "created_at": now.astimezone(
            timezone.utc
        ).isoformat()
    }

    result = supabase_request(
        "POST",
        "chat_messages",
        data=data
    )

    return result is not None


# =========================================================
# 오늘 내 소통량
# =========================================================

def get_my_count(group_id, user_id):
    today = get_today()

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

def get_ranking(group_id):
    today = get_today()

    params = {
        "select": "user_id,user_name",
        "group_id": f"eq.{group_id}",
        "date_kst": f"eq.{today}",
        "order": "id.asc",
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
        uid = row.get("user_id")
        name = row.get(
            "user_name",
            "사용자"
        )

        if uid not in users:
            users[uid] = {
                "name": name,
                "count": 0
            }

        users[uid]["count"] += 1
        users[uid]["name"] = name

    ranking = []

    for uid, info in users.items():
        ranking.append(
            (
                uid,
                info["name"],
                info["count"]
            )
        )

    ranking.sort(
        key=lambda x: x[2],
        reverse=True
    )

    return ranking


# =========================================================
# 방 통계
# =========================================================

def get_room_stats(group_id):

    # 총 메시지
    total_params = {
        "select": "id",
        "group_id": f"eq.{group_id}",
        "limit": "10000"
    }

    total_result = supabase_request(
        "GET",
        "chat_messages",
        params=total_params
    )

    total_messages = (
        len(total_result)
        if total_result
        else 0
    )

    # 참여 인원
    user_params = {
        "select": "user_id",
        "group_id": f"eq.{group_id}",
        "limit": "10000"
    }

    user_result = supabase_request(
        "GET",
        "chat_messages",
        params=user_params
    )

    participants = set()

    if user_result:
        for row in user_result:
            if row.get("user_id"):
                participants.add(
                    row["user_id"]
                )

    participant_count = len(participants)

    # 가장 활발했던 날
    day_params = {
        "select": "date_kst",
        "group_id": f"eq.{group_id}",
        "order": "id.asc",
        "limit": "10000"
    }

    day_result = supabase_request(
        "GET",
        "chat_messages",
        params=day_params
    )

    day_counts = {}

    if day_result:
        for row in day_result:
            day = row.get("date_kst")

            if day:
                day_counts[day] = (
                    day_counts.get(day, 0) + 1
                )

    if day_counts:
        active_day = max(
            day_counts.items(),
            key=lambda x: x[1]
        )
    else:
        active_day = None

    return {
        "total_messages": total_messages,
        "participants": participant_count,
        "active_day": active_day
    }


# =========================================================
# 내 통계
# =========================================================

def get_my_stats(group_id, user_id):

    # 전체 내 소통량
    my_params = {
        "select": "id",
        "group_id": f"eq.{group_id}",
        "user_id": f"eq.{user_id}",
        "limit": "10000"
    }

    my_result = supabase_request(
        "GET",
        "chat_messages",
        params=my_params
    )

    total_count = (
        len(my_result)
        if my_result
        else 0
    )

    # 전체 사용자 소통량
    all_params = {
        "select": "user_id,user_name",
        "group_id": f"eq.{group_id}",
        "limit": "10000"
    }

    all_result = supabase_request(
        "GET",
        "chat_messages",
        params=all_params
    )

    user_counts = {}

    if all_result:
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

    my_rank = None

    for index, (uid, count) in enumerate(
        sorted_users,
        start=1
    ):
        if uid == user_id:
            my_rank = index
            break

    if my_rank is None:
        my_rank = len(sorted_users) + 1

    # 가장 활발한 시간
    hour_params = {
        "select": "hour_kst",
        "group_id": f"eq.{group_id}",
        "user_id": f"eq.{user_id}",
        "limit": "10000"
    }

    hour_result = supabase_request(
        "GET",
        "chat_messages",
        params=hour_params
    )

    hour_counts = {}

    if hour_result:
        for row in hour_result:
            hour = row.get("hour_kst")

            if hour is not None:
                hour_counts[hour] = (
                    hour_counts.get(hour, 0) + 1
                )

    if hour_counts:
        active_hour, active_count = max(
            hour_counts.items(),
            key=lambda x: x[1]
        )

        next_hour = (
            active_hour + 1
        ) % 24

        active_time = (
            f"{active_hour:02d}시~"
            f"{next_hour:02d}시 "
            f"({active_count}개)"
        )
    else:
        active_time = "기록 없음"

    return {
        "total_count": total_count,
        "rank": my_rank,
        "active_time": active_time
    }


# =========================================================
# 숫자 → 위첨자
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
        for char in number
    )


# =========================================================
# 닉네임 생성
#
# 입력:
# 이름 성별 나이 돔/섭/스위치/바닐라
#
# 예:
# 윤아 여자 10 돔
# 철수 남자 09 섭
# =========================================================

def make_nickname(text):
    parts = text.strip().split()

    if len(parts) != 4:
        return None

    name = parts[0]
    gender = parts[1]
    age = parts[2]
    role = parts[3]

    # 나이 정확히 2자리
    if not re.fullmatch(
        r"\d{2}",
        age
    ):
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
        "select": "user_name,message",
        "group_id": f"eq.{group_id}",
        "order": "id.desc",
        "limit": str(
            RECENT_MESSAGE_LIMIT
        )
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
        name = row.get(
            "user_name",
            "사용자"
        )

        message = row.get(
            "message",
            ""
        )

        messages.append(
            f"{name}: {message}"
        )

    return messages


# =========================================================
# AI 판단
# =========================================================

def ask_ai_to_reply(group_id):

    if not OPENAI_API_KEY:
        return None

    recent_messages = get_recent_messages(
        group_id
    )

    if not recent_messages:
        return None

    context = "\n".join(
        recent_messages
    )

    prompt = f"""
너는 LINE 단체채팅방에 조용히 참여하는 친근한 사람 같은 봇이다.

최근 대화:
{context}

이 대화를 보고 지금 봇이 한마디 하는 것이 자연스러운 상황인지 판단해라.

중요:
- 평범한 대화에는 굳이 끼어들지 않는다.
- 억지로 대화를 만들지 않는다.
- 특정 키워드만 보고 판단하지 말고 전체 대화 흐름을 본다.
- 정말 자연스럽게 한마디 할 만할 때만 답한다.
- 민감한 주제, 정치, 종교, 성적인 이야기,
  개인정보, 심각한 싸움에는 개입하지 않는다.
- 답변은 짧고 자연스럽게 한다.
- AI처럼 말하지 않는다.
- 매번 비슷한 말을 반복하지 않는다.

답변 형식은 반드시 아래 둘 중 하나다.

개입하지 않는 경우:
NO

개입하는 경우:
YES|짧은 답변

설명은 절대 붙이지 마라.
"""

    headers = {
        "Authorization": (
            f"Bearer {OPENAI_API_KEY}"
        ),
        "Content-Type": "application/json"
    }

    payload = {
        "model": "gpt-5.6-luna",
        "input": prompt
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=15
        )

        if not response.ok:
            print(
                "OpenAI 오류:",
                response.status_code
            )
            print(response.text)
            return None

        data = response.json()

        output_text = data.get(
            "output_text"
        )

        if output_text:
            return output_text.strip()

        output = data.get(
            "output",
            []
        )

        texts = []

        for item in output:

            content = item.get(
                "content",
                []
            )

            for content_item in content:

                if content_item.get(
                    "type"
                ) == "output_text":

                    texts.append(
                        content_item.get(
                            "text",
                            ""
                        )
                    )

        if texts:
            return "".join(
                texts
            ).strip()

    except Exception as e:
        print(
            "OpenAI 연결 오류:",
            e
        )

    return None


# =========================================================
# AI 마지막 답변 시간
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

    value = result[0].get(
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


def update_last_ai_reply(group_id):

    now = datetime.now(
        timezone.utc
    ).isoformat()

    data = {
        "group_id": group_id,
        "last_ai_reply": now
    }

    url = (
        f"{SUPABASE_URL.rstrip('/')}"
        f"/rest/v1/bot_state"
    )

    headers = supabase_headers()

    headers["Prefer"] = (
        "resolution=merge-duplicates"
    )

    params = {
        "on_conflict": "group_id"
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            params=params,
            json=data,
            timeout=10
        )

        if not response.ok:
            print(
                "AI 상태 저장 오류:",
                response.text
            )

    except Exception as e:
        print(
            "AI 상태 저장 오류:",
            e
        )


# =========================================================
# AI 랜덤 개입
# =========================================================

def maybe_ai_intervene(group_id):

    if not OPENAI_API_KEY:
        return

    # 3% 확률
    if random.random() > AI_TRIGGER_CHANCE:
        return

    # 마지막 AI 답변 확인
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
            return

    result = ask_ai_to_reply(
        group_id
    )

    if not result:
        return

    if result == "NO":
        return

    if not result.startswith(
        "YES|"
    ):
        return

    reply = result[4:].strip()

    if not reply:
        return

    # 너무 긴 답변 방지
    if len(reply) > 300:
        reply = reply[:300]

    response = push_message(
        group_id,
        reply
    )

    if response and response.ok:
        update_last_ai_reply(
            group_id
        )


# =========================================================
# 홈
# =========================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():
    return (
        "LINE BOT IS RUNNING",
        200
    )


# =========================================================
# 웹훅
# =========================================================
# 중요:
# LINE Developers의 Webhook URL이
# https://line-bot-crry.onrender.com/webhook
# 이므로 반드시 /webhook이어야 함.
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook():

    # -----------------------------------------
    # LINE 서명 확인
    # -----------------------------------------

    signature = request.headers.get(
        "X-Line-Signature"
    )

    body = request.get_data()

    if not CHANNEL_SECRET:
        print("CHANNEL_SECRET이 없습니다.")
        abort(500)

    hash_value = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()

    expected_signature = (
        base64.b64encode(
            hash_value
        ).decode("utf-8")
    )

    if not hmac.compare_digest(
        expected_signature,
        signature or ""
    ):
        print("LINE 서명 검증 실패")
        abort(400)

    try:
        data = request.get_json(
            silent=True
        ) or {}
    except Exception:
        data = {}

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

        # 텍스트만 처리
        if message.get(
            "type"
        ) != "text":
            continue

        text = message.get(
            "text",
            ""
        ).strip()

        source = event.get(
            "source",
            {}
        )

        # 그룹 채팅만 처리
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

        # -----------------------------------------
        # 사용자 이름
        # -----------------------------------------

        user_name = get_profile_name(
            group_id,
            user_id
        )

        # -----------------------------------------
        # 닉네임 생성
        # -----------------------------------------

        nickname = make_nickname(
            text
        )

        if nickname:

            reply_message(
                reply_token,
                nickname
            )

            continue

        # -----------------------------------------
        # 명령어 공백 제거
        # -----------------------------------------

        normalized = re.sub(
            r"\s+",
            "",
            text
        )

        # -----------------------------------------
        # 내 마딧수
        # -----------------------------------------

        if normalized in [
            "내마딧수",
            "내소통량"
        ]:

            count = get_my_count(
                group_id,
                user_id
            )

            reply_message(
                reply_token,
                f"💬 {user_name}님의 "
                f"오늘 소통량: {count}개"
            )

            continue

        # -----------------------------------------
        # 단라 소통량
        # -----------------------------------------

        if normalized in [
            "단라소통량",
            "단라소통량순위"
        ]:

            ranking = get_ranking(
                group_id
            )

            if not ranking:

                reply_message(
                    reply_token,
                    "아직 오늘 소통량이 없어!"
                )

                continue

            lines = [
                "🏆 단라 소통량 순위"
            ]

            for index, (
                _,
                name,
                count
            ) in enumerate(
                ranking,
                start=1
            ):

                lines.append(
                    f"{index}위 "
                    f"{name} - {count}개"
                )

            reply_message(
                reply_token,
                "\n".join(lines)
            )

            continue

        # -----------------------------------------
        # 방 통계
        # -----------------------------------------

        if normalized == "방통계":

            stats = get_room_stats(
                group_id
            )

            active_day = stats[
                "active_day"
            ]

            if active_day:

                day_string, day_count = (
                    active_day
                )

                year, month, day = map(
                    int,
                    day_string.split("-")
                )

                active_text = (
                    f"{month}월 {day}일 "
                    f"({day_count}개)"
                )

            else:

                active_text = (
                    "기록 없음"
                )

            result = (
                "🏠 우리방 통계\n\n"
                f"💬 총 메시지: "
                f"{stats['total_messages']}개\n"
                f"👥 참여 인원: "
                f"{stats['participants']}명\n"
                f"🔥 가장 활발했던 날: "
                f"{active_text}"
            )

            reply_message(
                reply_token,
                result
            )

            continue

        # -----------------------------------------
        # 내 통계
        # -----------------------------------------

        if normalized == "내통계":

            stats = get_my_stats(
                group_id,
                user_id
            )

            result = (
                f"📊 {user_name}님의 통계\n\n"
                f"💬 총 소통량: "
                f"{stats['total_count']}개\n"
                f"🏆 방 내 순위: "
                f"{stats['rank']}위\n"
                f"🕐 가장 활발한 시간: "
                f"{stats['active_time']}"
            )

            reply_message(
                reply_token,
                result
            )

            continue

        # -----------------------------------------
        # 소통량 초기화
        # -----------------------------------------

        if normalized == "단라소통량초기화":

            today = get_today()

            params = {
                "group_id": f"eq.{group_id}",
                "date_kst": f"eq.{today}"
            }

            result = supabase_request(
                "DELETE",
                "chat_messages",
                params=params
            )

            if result is not None:

                reply_message(
                    reply_token,
                    "✅ 오늘 소통량을 초기화했어."
                )

            else:

                reply_message(
                    reply_token,
                    "❌ 초기화에 실패했어."
                )

            continue

        # -----------------------------------------
        # 일반 메시지
        # -----------------------------------------

        # ㅋㅋㅋㅋ / ㅎㅎ / ㅠㅠ 등 제외
        if is_laugh_only(text):
            continue

        # 메시지 저장
        saved = save_message(
            group_id,
            user_id,
            user_name,
            text
        )

        if not saved:
            print(
                "메시지 저장 실패"
            )

        # -----------------------------------------
        # AI 개입
        # -----------------------------------------

        if saved:

            maybe_ai_intervene(
                group_id
            )

    return "OK", 200


# =========================================================
# 서버 실행
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
