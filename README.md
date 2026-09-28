# Jovanica Manager — personal-profile PPV test

This is a working implementation to deploy, not an already-running service. No credentials are included. Live delivery and purchases have not been tested with your account.

## Deploy from your phone

1. Extract this ZIP using your phone's Files app.
2. In a browser, create a private GitHub repository named `jovanica-manager`. Upload the extracted `bot.py`, `Dockerfile`, and this README at the repository root. Upload the files, not the ZIP. GitHub's desktop-site view may help.
3. In Railway, create a NEW service from this GitHub repository. Do not overwrite your existing bot service. Railway detects the Dockerfile.
4. Add variable `BOT_TOKEN` using the token for `@Jovanicamm_bot` from BotFather. Keep it out of GitHub and screenshots. Add a volume mounted at `/data` to preserve connection history and drafts. Run only one replica; disable sleeping/serverless for continuous polling. No public domain or web port is needed.
5. Deploy. From Jovanica's personal Telegram account, open `@Jovanicamm_bot` and send `/id`. It returns that account's numeric ID.
6. Set Railway variable `OWNER_ID` to that numeric ID, then deploy the staged change. It must be the ID of the PERSONAL PROFILE connected in Chat Automation, not the bot ID and not the second/test account.
7. Keep Secretary Mode enabled and connect the manager to Jovanica's account. Allow replies; limit access to the second account's chat for testing. No permissions to transfer Stars, change profile details, or delete chats are needed.
8. AFTER deployment, send a fresh `Hi` from the second account to Jovanica's personal profile. The manager records incoming chat metadata, not the message text. The connection is fetched automatically from that incoming message.

## First paid post

From Jovanica's personal account, in your PRIVATE CHAT WITH THE MANAGER BOT:

- Send `/chats` and copy the numeric ID of your second account.
- Send `/target 123456789` using that actual ID.
- Upload ONE ordinary test photo or video, optionally with a caption. Do not send an album or file/document.
- Send `/price 1` for a one-Star test.
- Send `/send`. Check the recipient, Stars price and caption.
- Send the displayed `/confirm CODE` to send it from Jovanica's profile.
- Switch to the second account and verify sender, locked preview and price. Unlocking spends real Stars; viewing the locked preview does not.

## Limits and behavior

- This MVP sends one photo/video to one selected recipient. It does not implement albums, AI chat, welcome offers, broadcasts, buyer analytics, or refunds.
- Official connected-bot replies require a recent incoming message from the recipient. The bot applies a conservative 23h59m window and Telegram independently enforces eligibility. The window is not renewed by Jovanica's outgoing messages.
- Public `/id` returns only the caller's own ID. All management commands require OWNER_ID. Connections belonging to any other account are rejected.
- Confirmation expires after five minutes and is consumed before sending. Uncertain sends are NEVER automatically retried. Check the recipient chat before manually retrying.
- Polling update offsets are saved before processing to avoid replaying paid sends after a crash. A crash can lose one incoming event/command; resend it if needed.
- Telegram's sendPaidMedia documentation credits non-channel paid-media proceeds to the bot balance. Use your own dedicated bot and verify the balance during testing.
- Your personal-profile avatar/name is the sender identity. Telegram may show automation attribution. This does not guarantee an invisible bot or prove which setup another creator uses.
- No support for bypassing Telegram reply windows or messaging people who have not contacted the profile.

## Troubleshooting

- `/id` does not reply: check Railway deployment logs and BOT_TOKEN. The connection banner alone does not mean code is running.
- `/id` works but management does not: OWNER_ID must be Jovanica's numeric personal-profile ID; deploy after changing it.
- `/chats` empty: send Hi AFTER the software is running; check selected chats and connection/reply permission.
- Polling repeatedly fails: check token and make sure no other process uses it. Existing webhooks are not automatically removed.
- Send fails: check connection, reply permission, selected chat and fresh incoming message. Telegram is the final authority on eligibility.

Sources:
https://core.telegram.org/bots/api#sendpaidmedia
https://core.telegram.org/bots/api#businessbotrights
https://core.telegram.org/bots/features#secretary-bots
https://docs.railway.com/quick-start
https://docs.railway.com/builds/dockerfiles
https://docs.railway.com/variables
https://docs.railway.com/volumes
