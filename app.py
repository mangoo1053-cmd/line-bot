from flask import Flask, request, abort
import requests
import hashlib
import hmac
import base64
import json
import os
import re

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ.get(CHANNEL_ACCESS_TOKEN)
CHANNEL_SECRET = os.environ.get(CHANNEL_SECRET)

DATA_FILE = chat_data.json


# -------------------------
# 데이터
# -------------------------

def load_data():
    if not os.path.exists(DATA_FILE)
        return {}

    try
        with open(DATA_FILE, r, encoding=utf-8) as f
            return json.load(f)
    except
        return {}


def save_data(data)
    with open(DATA_FILE, w, encoding=utf-8) as f
        json.dump(data, f, ensure_ascii=False, indent=2)


# -------------------------
# LINE API
# -------------------------

def line_api(method, url, data=None)

    headers = {
        Authorization fBearer {CHANNEL_ACCESS_TOKEN},
        Content-Type applicationjson
    }

    if method == POST
        return requests.post(
            url,
            headers=headers,
            json=data
        )

    return requests.get(
        url,
        headers=headers
    )


def reply_message(reply_token, text)

    url = httpsapi.line.mev2botmessagereply

    data = {
        replyToken reply_token,
        messages [
            {
                type text,
                text text
            }
        ]
    }

    response = line_api(POST, url, data)

    print(LINE reply, response.status_code, response.text)


# -------------------------
# 사용자 이름
# -------------------------

def get_user_name(group_id, user_id)

    url = (
        fhttpsapi.line.mev2botgroup
        f{group_id}member{user_id}
    )

    response = line_api(GET, url)

    if response.status_code == 200
        return response.json().get(
            displayName,
            알 수 없음
        )

    return 알 수 없음


# -------------------------
# ㅋㅋㅋ  ㅎㅎㅎ만 있는 메시지 제외
# -------------------------

def is_laugh_only(text)

    text = text.strip()

    if not text
        return True

    if re.fullmatch(r^[ㅋㅎㅠㅜ]+$, text)
        return True

    return False


# -------------------------
# 메시지 +1
# -------------------------

def add_message(group_id, user_id)

    data = load_data()

    if group_id not in data
        data[group_id] = {
            users {}
        }

    if user_id not in data[group_id][users]

        name = get_user_name(
            group_id,
            user_id
        )

        data[group_id][users][user_id] = {
            name name,
            count 0
        }

    data[group_id][users][user_id][count] += 1

    save_data(data)


# -------------------------
# 순위
# -------------------------

def get_ranking(group_id)

    data = load_data()

    if group_id not in data
        return 아직 집계된 채팅이 없어.

    users = data[group_id][users]

    if not users
        return 아직 집계된 채팅이 없어.

    ranking = sorted(
        users.values(),
        key=lambda x x[count],
        reverse=True
    )

    result = "🏆 단라 소통량 순위\n\n"

    for i, user in enumerate(ranking, start=1)

        result += (
            f"{i}위 {user['name']} — 
            {user['count']:,}개\n"
        )

    return result


# -------------------------
# 웹훅
# -------------------------

@app.route(webhook, methods=[POST])
def webhook()

    signature = request.headers.get(
        X-Line-Signature
    )

    body = request.get_data(
        as_text=True
    )

    # LINE 서명 확인
    hash_value = hmac.new(
        CHANNEL_SECRET.encode(utf-8),
        body.encode(utf-8),
        hashlib.sha256
    ).digest()

    expected_signature = base64.b64encode(
        hash_value
    ).decode(utf-8)

    if not hmac.compare_digest(
        expected_signature,
        signature or 
    )
        abort(400)

    data = request.json

    for event in data.get(events, [])

        if event.get(type) != message
            continue

        message = event.get(
            message,
            {}
        )

        message_type = message.get(
            type
        )

        source = event.get(
            source,
            {}
        )

        # 그룹방만 처리
        if source.get(type) != group
            continue

        group_id = source.get(
            groupId
        )

        user_id = source.get(
            userId
        )

        if not user_id
            continue

        # ---------------------
        # 텍스트
        # ---------------------

        if message_type == text

            text = message.get(
                text,
                
            ).strip()

            # 소통량
            if text in [
                단라 소통량,
                단라소통량
            ]

                ranking = get_ranking(
                    group_id
                )

                reply_message(
                    event[replyToken],
                    ranking
                )

                continue

            # 초기화
            if text in [
                단라 소통량 초기화,
                단라소통량 초기화
            ]

                all_data = load_data()

                all_data[group_id] = {
                    users {}
                }

                save_data(all_data)

                reply_message(
                    event[replyToken],
                    ✅ 소통량 집계를 초기화했어.
                )

                continue

            # ㅋㅋㅋ  ㅎㅎㅎ만 있는 경우
            if is_laugh_only(text)
                continue

            # 일반 텍스트 +1
            add_message(
                group_id,
                user_id
            )

        # 사진  스티커  영상 등
        else
            continue

    return OK


# -------------------------
# 서버 실행
# -------------------------

@app.route()
def home()
    return LINE Bot is running!


if __name__ == __main__

    port = int(
        os.environ.get(
            PORT,
            10000
        )
    )

    app.run(
        host=0.0.0.0,
        port=port
    )
