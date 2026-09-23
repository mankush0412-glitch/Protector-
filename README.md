# Protector Bot

Compact Telegram protected-link bot with a Telegram WebApp Force Join flow.

Flow:

`Start → Open Link → Join/Verify channels → alternate Monetag ad → final unlock → destination`

Features:

- TEAM LEADER branding with Orbitron/Rajdhani styling.
- Multiple channel/group cards with optional image, name, Join and Verify.
- Real Telegram membership checks and persistent progress.
- Monetag in-app ads; `AD_JOIN_FREQUENCY=2` means 1st/3rd/5th verification.
- The destination is returned only after the server-side final ad session
  completes; `AD_MIN_SECONDS=8` is the default protection wait.
- Final ad unlock; destination is also preloaded in the frontend like the old flow.
- Native Telegram Share Friend link.
- No spin, points, referrals, wallets, payments or giveaways.

Required environment:

`BOT_TOKEN`/`TELEGRAM_TOKEN`, `MONGO_URL`/`MONGODB_URI`,
`RENDER_EXTERNAL_URL`, `ADMIN_USER_ID`/`ADMIN_ID`.

Optional configuration:

`SUPPORT_CHANNELS=@one,@two`

`SUPPORT_CHANNELS_CONFIG=[{"id":"@one","name":"Updates","image":"https://...","url":"https://t.me/..."}]`

`MONETAG_ZONE_ID`, `START_LECTURE_URL`, `BRAND_TITLE`,
`AD_JOIN_FREQUENCY`, `AD_MIN_SECONDS`