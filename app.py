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
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)


# =========================================================
# 환경변수
# =========================================================

CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

DATA_FILE = "chat_data.json"


# =========================================================
# AI 설정
# =========================================================

# AI가 일반 대화에 개입할 확률
# 0.005 = 0.5%
AI_TRIGGER_CHANCE = 0.005

# AI가 한 번 개입한 후 3시간 동안 다시 개입하지 않음
AI_COOLDOWN_SECONDS = 3 * 60 * 60

# AI에게 보여줄 최근 대화 개수
RECENT_MESSAGE_LIMIT = 12


# =========================================================
# 한국 시간
# =========================================================

def get_now():

    return datetime.now(
        ZoneInfo("Asia/Seoul")
    )


def get_today():

    return get_now().strftime("%Y-%m-%d")


def get_current_hour():

    return get_now().hour


# =========================================================
# 데이터 기본 구조
# =========================================================

def create_group_data():

    return {
        "date": get_today(),

        # 오늘 소통량
        "users": {},

        # 지금까지 전체 메시지
        "total_messages": 0,

        # 지금까지 참여한 사람
        "participants": {},

        # 날짜별 전체 메시지
        "daily_counts": {},

        # 시간별 사용자 메시지
        "hour_counts": {},

        # 최근 대화
        "recent_messages": [],

        # 마지막 AI 개입 시간
        "last_ai_reply": 0
    }


# =========================================================
# 데이터 불러오기
# =========================================================

def load_data():

    if not os.path.exists(DATA_FILE):
        return {}

    try:

        with open(
            DATA_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return {}


# =========================================================
# 데이터 저장
# =========================================================

def save_data(data):

    with open(
        DATA_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            indent=2
        )


# =========================================================
# 그룹 데이터 확인
# =========================================================

def check_new_day(group_id):

    data = load_data()

    # 처음 들어온 방
    if group_id not in data:

        data[group_id] = create_group_data()

        save_data(data)

        return data


    group = data[group_id]


    # 기존 데이터에 새 항목이 없을 경우 보완
    if "total_messages" not in group:
        group["total_messages"] = 0

    if "participants" not in group:
        group["participants"] = {}

    if "daily_counts" not in group:
        group["daily_counts"] = {}

    if "hour_counts" not in group:
        group["hour_counts"] = {}

    if "recent_messages" not in group:
        group["recent_messages"] = []

    if "last_ai_reply" not in group:
        group["last_ai_reply"] = 0

    if "users" not in group:
        group["users"] = {}


    today = get_today()


    # 날짜가 바뀌었으면 오늘 소통량만 초기화
    if group.get("date") != today:

        group["date"] = today
        group["users"] = {}
        group["hour_counts"] = {}


    save_data(data)

    return data


# =========================================================
# 닉네임 만들기
# =========================================================

def make_nickname(text):

    parts = text.strip().split()


    # 이름 / 나이 / 타입 / 성별
    if len(parts) != 4:
        return None


    name, age, role, gender = parts


    # 나이는 정확히 두 자리
    if not age.isdigit() or len(age) != 2:
        return None


    # 타입
    if role == "돔":

        role_text = "𝒅"

    elif role == "섭":

        role_text = "𝒔"

    elif role == "스위치":

        role_text = "𝒔/𝒅"

    elif role == "바닐라":

        role_text = "𝒗"

    else:

        return None


    # 성별
    if gender == "여자":

        gender_icon = "📡"

    elif gender == "남자":

        gender_icon = "🔭"

    else:

        return None


    # 숫자를 위첨자로 변환
    superscript = str.maketrans(
        "0123456789",
        "⁰¹²³⁴⁵⁶⁷⁸⁹"
    )


    age_sup = age.translate(
        superscript
    )


    # 언더바 없음
    return f"{name}{role_text}{age_sup}{gender_icon}"


# =========================================================
# LINE API
# =========================================================

def line_api(method, url, data=None):

    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }


    if method == "POST":

        return requests.post(
            url,
            headers=headers,
            json=data,
            timeout=10
        )


    return requests.get(
        url,
        headers=headers,
        timeout=10
    )


# =========================================================
# LINE 메시지 답장
# =========================================================

def reply_message(reply_token, text):

    url = (
        "https://api.line.me/"
        "v2/bot/message/reply"
    )


    data = {

        "replyToken": reply_token,

        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }


    try:

        response = line_api(
            "POST",
            url,
            data
        )


        print(
            "LINE reply:",
            response.status_code,
            response.text
        )


    except Exception as e:

        print(
            "LINE reply error:",
            e
        )


# =========================================================
# 사용자 이름 가져오기
# =========================================================

def get_user_name(group_id, user_id):

    url = (
        f"https://api.line.me/v2/bot/group/"
        f"{group_id}/member/{user_id}"
    )


    try:

        response = line_api(
            "GET",
            url
        )


        if response.status_code == 200:

            return response.json().get(
                "displayName",
                "알 수 없음"
            )

    except Exception as e:

        print(
            "사용자 이름 가져오기 오류:",
            e
        )


    return "알 수 없음"


# =========================================================
# ㅋㅋㅋ / ㅎㅎㅎ / ㅠㅠ / ㅜㅜ만 있는 메시지 제외
# =========================================================

def is_laugh_only(text):

    text = text.strip()


    if not text:

        return True


    if re.fullmatch(
        r"^[ㅋㅎㅠㅜ]+$",
        text
    ):

        return True


    return False


# =========================================================
# 메시지 기록
# =========================================================

def add_message(
    group_id,
    user_id,
    text,
    user_name
):

    data = check_new_day(
        group_id
    )


    group = data[group_id]


    # -----------------------------------------------------
    # 오늘 사용자
    # -----------------------------------------------------

    if user_id not in group["users"]:

        group["users"][user_id] = {

            "name": user_name,

            "count": 0,

            "hour_counts": {}
        }


    group["users"][user_id]["count"] += 1


    # -----------------------------------------------------
    # 전체 메시지
    # -----------------------------------------------------

    group["total_messages"] += 1


    # -----------------------------------------------------
    # 참여 인원
    # -----------------------------------------------------

    group["participants"][user_id] = user_name


    # -----------------------------------------------------
    # 날짜별 메시지
    # -----------------------------------------------------

    today = get_today()


    if today not in group["daily_counts"]:

        group["daily_counts"][today] = 0


    group["daily_counts"][today] += 1


    # -----------------------------------------------------
    # 시간별 메시지
    # -----------------------------------------------------

    hour = get_current_hour()

    hour_key = str(hour)


    if hour_key not in group["hour_counts"]:

        group["hour_counts"][hour_key] = 0


    group["hour_counts"][hour_key] += 1


    # 사용자별 시간 기록
    if "hour_counts" not in group["users"][user_id]:

        group["users"][user_id]["hour_counts"] = {}


    user_hour_counts = group["users"][user_id]["hour_counts"]


    if hour_key not in user_hour_counts:

        user_hour_counts[hour_key] = 0


    user_hour_counts[hour_key] += 1


    # -----------------------------------------------------
    # 최근 대화 저장
    # -----------------------------------------------------

    group["recent_messages"].append({

        "name": user_name,

        "text": text,

        "time": get_now().strftime("%H:%M:%S")
    })


    # 최근 12개만 유지
    group["recent_messages"] = (
        group["recent_messages"][-RECENT_MESSAGE_LIMIT:]
    )


    save_data(data)


# =========================================================
# 내 오늘 소통량
# =========================================================

def get_my_count(
    group_id,
    user_id
):

    data = check_new_day(
        group_id
    )


    users = data[group_id]["users"]


    if user_id not in users:

        return 0


    return users[user_id].get(
        "count",
        0
    )


# =========================================================
# 오늘 소통량 순위
# =========================================================

def get_ranking(group_id):

    data = check_new_day(
        group_id
    )


    users = data[group_id]["users"]


    if not users:

        return "오늘 아직 집계된 채팅이 없어."


    ranking = sorted(
        users.items(),
        key=lambda x: x[1].get("count", 0),
        reverse=True
    )


    result = (
        "🏆 오늘의 단라 소통량 순위\n\n"
    )


    for i, (user_id, user) in enumerate(
        ranking,
        start=1
    ):

        result += (
            f"{i}위 {user.get('name', '알 수 없음')} — "
            f"{user.get('count', 0):,}개\n"
        )


    return result


# =========================================================
# 방 통계
# =========================================================

def get_room_stats(group_id):

    data = check_new_day(
        group_id
    )


    group = data[group_id]


    total_messages = group.get(
        "total_messages",
        0
    )


    participants = len(
        group.get(
            "participants",
            {}
        )
    )


    daily_counts = group.get(
        "daily_counts",
        {}
    )


    # 가장 활발했던 날
    if daily_counts:

        most_active_day = max(
            daily_counts.items(),
            key=lambda x: x[1]
        )

        date_text = most_active_day[0]
        message_count = most_active_day[1]


        try:

            date_obj = datetime.strptime(
                date_text,
                "%Y-%m-%d"
            )

            active_day_text = (
                f"{date_obj.month}월 "
                f"{date_obj.day}일 "
                f"({message_count:,}개)"
            )

        except Exception:

            active_day_text = (
                f"{date_text} "
                f"({message_count:,}개)"
            )

    else:

        active_day_text = "아직 기록 없음"


    return (
        "🏠 우리방 통계\n\n"

        f"💬 총 메시지: "
        f"{total_messages:,}개\n"

        f"👥 참여 인원: "
        f"{participants:,}명\n"

        f"🔥 가장 활발했던 날: "
        f"{active_day_text}"
    )


# =========================================================
# 내 통계
# =========================================================

def get_my_stats(
    group_id,
    user_id
):

    data = check_new_day(
        group_id
    )


    group = data[group_id]

    users = group.get(
        "users",
        {}
    )


    user_name = get_user_name(
        group_id,
        user_id
    )


    # 전체 누적 소통량
    total_count = 0


    if user_id in users:

        total_count = users[user_id].get(
            "count",
            0
        )


    # -----------------------------------------------------
    # 방 내 순위
    # -----------------------------------------------------

    sorted_users = sorted(
        users.items(),
        key=lambda x: x[1].get("count", 0),
        reverse=True
    )


    my_rank = None


    for index, (uid, user) in enumerate(
        sorted_users,
        start=1
    ):

        if uid == user_id:

            my_rank = index

            break


    if my_rank is None:

        my_rank_text = "아직 집계 없음"

    else:

        my_rank_text = f"{my_rank}위"


    # -----------------------------------------------------
    # 가장 활발한 시간
    # -----------------------------------------------------

    my_hour_counts = {}


    if user_id in users:

        my_hour_counts = users[user_id].get(
            "hour_counts",
            {}
        )


    if my_hour_counts:

        most_active_hour = max(
            my_hour_counts.items(),
            key=lambda x: x[1]
        )


        hour = int(
            most_active_hour[0]
        )


        count = most_active_hour[1]


        next_hour = (
            hour + 1
        ) % 24


        active_time_text = (
            f"{hour:02d}시~"
            f"{next_hour:02d}시 "
            f"({count:,}개)"
        )

    else:

        active_time_text = "아직 기록 없음"


    return (
        f"📊 {user_name}님의 통계\n\n"

        f"💬 총 소통량: "
        f"{total_count:,}개\n"

        f"🏆 방 내 순위: "
        f"{my_rank_text}\n"

        f"🕐 가장 활발한 시간: "
        f"{active_time_text}"
    )


# =========================================================
# AI에게 최근 대화 판단시키기
# =========================================================

def ask_ai_to_reply(group_id):

    if not OPENAI_API_KEY:

        print(
            "OPENAI_API_KEY가 없음"
        )

        return None


    data = check_new_day(
        group_id
    )


    group = data[group_id]


    recent_messages = group.get(
        "recent_messages",
        []
    )


    if not recent_messages:

        return None


    # 최근 대화 만들기
    conversation_lines = []


    for message in recent_messages:

        name = message.get(
            "name",
            "알 수 없음"
        )

        text = message.get(
            "text",
            ""
        )


        conversation_lines.append(
            f"{name}: {text}"
        )


    conversation = "\n".join(
        conversation_lines
    )


    # -----------------------------------------------------
    # AI 프롬프트
    # -----------------------------------------------------

    prompt = f"""
너는 한국인 친구들이 있는 LINE 단체채팅방에 자연스럽게 섞여 있는
아주 조용한 채팅봇이야.

아래 최근 대화를 전체적인 맥락으로 보고 판단해.

중요:
- 특정 키워드만 보고 판단하지 마.
- 대화의 흐름과 앞뒤 맥락을 전체적으로 봐.
- 평범하게 대화가 이어지고 있다면 억지로 끼어들지 마.
- 정말 자연스럽게 한마디 정도 끼어들 만한 상황일 때만 말해.
- 봇이라는 느낌이 강한 말투를 사용하지 마.
- 친구가 단톡방에서 한마디 하는 것처럼 짧고 자연스럽게 말해.
- 매번 비슷한 표현을 반복하지 마.
- 너무 길게 말하지 마.
- 질문을 남발하지 마.
- 민감한 개인정보 이야기는 하지 마.
- 성적인 내용, 정치, 종교, 심각한 갈등이나 싸움에는 개입하지 마.
- 사람을 공격하거나 비꼬는 말은 하지 마.
- 억지로 웃기려고 하지 마.

판단 결과는 반드시 다음 형식 중 하나만 사용해.

개입할 필요가 없으면:
NO

자연스럽게 한마디 할 만하면:
YES|하고 싶은 말

최근 대화:
{conversation}
"""


    url = (
        "https://api.openai.com/v1/responses"
    )


    headers = {

        "Authorization":
            f"Bearer {OPENAI_API_KEY}",

        "Content-Type":
            "application/json"
    }


    payload = {

        "model": "gpt-5.6-luna",

        "input": prompt,

        "max_output_tokens": 100
    }


    try:

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10
        )


        print(
            "OpenAI status:",
            response.status_code
        )


        if response.status_code != 200:

            print(
                "OpenAI error:",
                response.text
            )

            return None


        result = response.json()


        # Responses API 결과 추출
        ai_text = ""


        if "output_text" in result:

            ai_text = result["output_text"]


        else:

            for item in result.get(
                "output",
                []
            ):

                for content in item.get(
                    "content",
                    []
                ):

                    if content.get(
                        "type"
                    ) == "output_text":

                        ai_text += content.get(
                            "text",
                            ""
                        )


        ai_text = ai_text.strip()


        if not ai_text:

            return None


        print(
            "AI result:",
            ai_text
        )


        # -------------------------------------------------
        # NO
        # -------------------------------------------------

        if ai_text.upper() == "NO":

            return None


        # -------------------------------------------------
        # YES|내용
        # -------------------------------------------------

        if ai_text.startswith(
            "YES|"
        ):

            reply = ai_text[
                len("YES|"):
            ].strip()


            if not reply:

                return None


            # 너무 긴 답변 방지
            if len(reply) > 150:

                reply = reply[:150]


            return reply


        return None


    except Exception as e:

        print(
            "OpenAI error:",
            e
        )

        return None


# =========================================================
# AI 개입
# =========================================================

def maybe_ai_intervene(
    group_id,
    reply_token
):

    # API 키가 없으면 작동하지 않음
    if not OPENAI_API_KEY:

        return


    data = check_new_day(
        group_id
    )


    group = data[group_id]


    # -----------------------------------------------------
    # 3시간 쿨다운
    # -----------------------------------------------------

    last_ai_reply = group.get(
        "last_ai_reply",
        0
    )


    current_time = time.time()


    if (
        current_time - last_ai_reply
        < AI_COOLDOWN_SECONDS
    ):

        return


    # -----------------------------------------------------
    # 0.5% 확률
    # -----------------------------------------------------

    if random.random() > AI_TRIGGER_CHANCE:

        return


    # -----------------------------------------------------
    # AI에게 판단
    # -----------------------------------------------------

    ai_reply = ask_ai_to_reply(
        group_id
    )


    if not ai_reply:

        return


    # -----------------------------------------------------
    # AI 개입 시간 저장
    # -----------------------------------------------------

    data = load_data()


    if group_id not in data:

        return


    data[group_id]["last_ai_reply"] = time.time()


    save_data(data)


    # -----------------------------------------------------
    # AI 메시지 보내기
    # -----------------------------------------------------

    reply_message(
        reply_token,
        f"🤖 {ai_reply}"
    )


# =========================================================
# 웹훅
# =========================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def webhook():

    signature = request.headers.get(
        "X-Line-Signature"
    )


    body = request.get_data(
        as_text=True
    )


    # -----------------------------------------------------
    # LINE 서명 확인
    # -----------------------------------------------------

    if not CHANNEL_SECRET:

        abort(500)


    hash_value = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body.encode("utf-8"),
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

        abort(400)


    data = request.json


    for event in data.get(
        "events",
        []
    ):

        # 메시지 이벤트만
        if event.get(
            "type"
        ) != "message":

            continue


        message = event.get(
            "message",
            {}
        )


        message_type = message.get(
            "type"
        )


        source = event.get(
            "source",
            {}
        )


        # -------------------------------------------------
        # 그룹방만 처리
        # -------------------------------------------------

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


        if not user_id:

            continue


        # -------------------------------------------------
        # 텍스트 메시지만 처리
        # -------------------------------------------------

        if message_type != "text":

            # 사진 / 스티커 / 영상 등은 집계하지 않음
            continue


        text = message.get(
            "text",
            ""
        ).strip()


        if not text:

            continue


        # -------------------------------------------------
        # 사용자 이름
        # -------------------------------------------------

        user_name = get_user_name(
            group_id,
            user_id
        )


        # =================================================
        # 닉네임 변환
        # =================================================

        nickname = make_nickname(
            text
        )


        if nickname:

            reply_message(
                event["replyToken"],
                nickname
            )

            continue


        # =================================================
        # 내 마딧수
        # =================================================

        if text in [
            "내 마딧수",
            "내마딧수"
        ]:

            my_count = get_my_count(
                group_id,
                user_id
            )


            reply_message(
                event["replyToken"],

                f"📊 {user_name}님의 "
                f"오늘 소통량은 "
                f"{my_count:,}개야."
            )


            continue


        # =================================================
        # 단라 소통량
        # =================================================

        if text in [
            "단라 소통량",
            "단라소통량"
        ]:

            ranking = get_ranking(
                group_id
            )


            reply_message(
                event["replyToken"],
                ranking
            )


            continue


        # =================================================
        # 방 통계
        # =================================================

        if text in [
            "방 통계",
            "방통계"
        ]:

            stats = get_room_stats(
                group_id
            )


            reply_message(
                event["replyToken"],
                stats
            )


            continue


        # =================================================
        # 내 통계
        # =================================================

        if text in [
            "내 통계",
            "내통계"
        ]:

            stats = get_my_stats(
                group_id,
                user_id
            )


            reply_message(
                event["replyToken"],
                stats
            )


            continue


        # =================================================
        # 소통량 초기화
        # =================================================

        if text in [
            "단라 소통량 초기화",
            "단라소통량 초기화"
        ]:

            all_data = load_data()


            if group_id not in all_data:

                all_data[group_id] = (
                    create_group_data()
                )


            # 오늘 데이터만 초기화
            all_data[group_id]["date"] = (
                get_today()
            )


            all_data[group_id]["users"] = {}


            all_data[group_id]["hour_counts"] = {}


            save_data(
                all_data
            )


            reply_message(
                event["replyToken"],
                "✅ 오늘 소통량 집계를 초기화했어."
            )


            continue


        # =================================================
        # 웃음 / 울음만 있는 메시지는 집계 안 함
        # =================================================

        if is_laugh_only(
            text
        ):

            continue


        # =================================================
        # 일반 메시지 집계
        # =================================================

        add_message(
            group_id,
            user_id,
            text,
            user_name
        )


        # =================================================
        # AI 자연스러운 개입
        #
        # 확률 0.5%
        # 쿨다운 3시간
        # =================================================

        maybe_ai_intervene(
            group_id,
            event["replyToken"]
        )


    return "OK"


# =========================================================
# 홈페이지
# =========================================================

@app.route("/")
def home():

    return "LINE Bot is running!"


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
