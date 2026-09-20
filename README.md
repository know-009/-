import requests

# 1, 2단계에서 받은 값 입력
BOT_TOKEN = "8883150028:AAGjdkPCAebuWFTq302Q6I8wd0hsFdUS6Wo"  # 본인의 Bot Token
CHAT_ID = "8539124350"            # 본인의 Chat ID

text_message = "🚀 텔레그램 알림봇 테스트 메시지입니다!"

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
params = {
    "chat_id": CHAT_ID,
    "text": text_message
}

response = requests.post(url, json=params)
print(response.json())
