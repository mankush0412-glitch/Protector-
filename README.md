# Protector Bot — merged 16 KB base + vetted 18 KB fixes

Compact Telegram protected-link bot with a Telegram WebApp Force Join flow.

Flow:

`Start → Open Link → Join/Verify channels → bounded Monetag ad flow → Open Link → destination`

Features:

- TEAM LEADER branding with Orbitron/Rajdhani styling.
- CA-style full-screen force-join gate with real Telegram channel/group names,
  profile photos, Join buttons, Joined states, and one bottom Verify action.
- Real Telegram membership checks and persistent progress.
- Monetag in-app ads after every second newly verified join (1st, 3rd, 5th),
  after the final Verify,
  once when an already-joined user opens the mini app, and once more when
  that user clicks Open Link.
- Channel metadata is resolved from Telegram at runtime when names, links, or
  images are not supplied in `SUPPORT_CHANNELS_CONFIG`.
- The destination is returned only after the server-side final ad session
  completes; `AD_MIN_SECONDS=1` is the default protection wait.
- Final ad unlock; the destination is returned only after the server-side
  session is complete.
- Native Telegram Share button sits under Open Link on the bot start message.
- The WebApp has no duplicate Share control.
- The exact Other Lectures link is
  `https://telegra.ph/𝗟𝗘𝗖𝗧𝗨𝗥𝗘𝗦-𝗟𝗜𝗦𝗧-03-24-2`.
- No spin, points, referrals, wallets, payments or giveaways.

Required environment:

`BOT_TOKEN`/`TELEGRAM_TOKEN`, `MONGO_URL`/`MONGODB_URI`,
`RENDER_EXTERNAL_URL`, `ADMIN_USER_ID`/`ADMIN_ID`.

Optional configuration:

`SUPPORT_CHANNELS=@one,@two`

`SUPPORT_CHANNELS_CONFIG=[{"id":"@one","name":"Updates","image":"https://...","url":"https://t.me/..."}]`

The `name`, `image`, and `url` properties are optional. Telegram `getChat`
fills them when the bot has permission to read the channel. `image` can also
be a stable CDN URL if you prefer to provide the DP yourself.

`MONETAG_ZONE_ID`, `START_LECTURE_URL`, `LECTURE_LABEL`, `BRAND_TITLE`,
`AD_JOIN_FREQUENCY`, `AD_MIN_SECONDS`

`AD_JOIN_FREQUENCY=2` is the shipped setting: the 1st newly verified join
shows an ad, the 2nd skips it, and the 3rd shows it again. `AD_MIN_SECONDS=1`
keeps the old fast ad feel while retaining a small server-side session check.

## Merge notes

- The original 16 KB bot commands, protected-link storage, broadcast tools,
  admin commands, and deployment layout remain the base.
- The useful 18 KB changes are server-side Telegram WebApp authentication,
  configurable channel cards, persistent verification progress, and bounded
  server-side ad sessions.
- Required-channel membership is checked before unlock. Already-joined users
  skip the force-join gate and go directly through the original final
  ad/open flow.
- Ad calls are capped and fail open after a timeout so a missing ad fill cannot
  strand the user.