#!/usr/bin/env python3
"""测试通过 AppleScript 给 woodyterran@gmail.com 发 iMessage"""
import subprocess

def send_imessage_text(text):
    """直接通过 AppleScript 发送 iMessage"""
    if not text:
        return
    try:
        safe_text = text.replace('"', '\\"')
        script = f'''
        tell application "Messages"
            set targetService to 1st account whose service type = iMessage
            set targetBuddy to participant "woodyterran@gmail.com" of targetService
            send "{safe_text}" to targetBuddy
        end tell
        '''
        subprocess.run(["osascript", "-e", script], check=True)
        print("✅ 测试消息已发送")
    except Exception as e:
        print(f"❌ 发送失败: {e}")

if __name__ == "__main__":
    send_imessage_text("【测试】iMessage 地址已改为邮箱，请确认收到此消息。")