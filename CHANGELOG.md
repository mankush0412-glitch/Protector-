# Changelog

## Final merge

### What changed

- Used the 16 KB Protector bot as the functional base.
- Kept the 18 KB improvements that are useful for this flow:
  - Telegram WebApp `initData` authentication.
  - Configurable channel/group cards with names, images, and invite URLs.
  - Persistent per-user channel-verification progress.
  - Server-side, expiring ad sessions before destination unlock.
  - `BOT_TOKEN`/`TELEGRAM_TOKEN` and `MONGO_URL`/`MONGODB_URI` compatibility.
- Kept the original protected-link, revoke, broadcast, statistics, help, and
  lecture-admin commands.
- Removed the accidental duplicate broadcast delivery call.
- Normalized ad-session expiry checks to UTC-aware datetimes.

### Preserved from the 16 KB original

- The bot command set and MongoDB collections.
- Protected-link creation and revocation behavior.
- The original Telegram webhook and Render deployment structure.
- Team Leader branding, fancy text styling, support-channel presentation, and
  the existing “create protected link” entry point.
- The intended sequence of opening the protected Telegram destination only after
  the access flow completes.

### Force-join and verification

- Required channels/groups are checked before access unlock.
- Each configured channel gets a Join and Verify card in the WebApp.
- Membership is checked server-side using the Telegram Bot API.
- Verification progress is saved per user and per protected-link token.
- If all required channels are already joined, the join cards are skipped.
- If a user has not joined a required channel, the user remains in the
  force-join screen and the destination cannot be unlocked.

### Ads and rotation flow

- Monetag in-app ads keep the existing fullscreen/in-app configuration.
- Direct Open for an already-joined user runs a bounded two-step sequence:
  one automatic ad step and one final server-validated unlock step.
- Completing the last channel verification can run the configured verification
  ad step and then the final unlock step.
- SDK absence, no-fill, or a hung ad call cannot trap the user indefinitely;
  the client uses a timeout while the server still enforces the minimum ad
  session duration.
- Repeated clicks cannot reuse an ad session because the server claims it
  atomically.

### Share and lecture link

- Added Telegram’s native Share button directly below Open Link in the bot
  start/protected-link messages.
- The Share button uses exactly:
  `https://telegra.ph/𝗟𝗘𝗖𝗧𝗨𝗥𝗘𝗦-𝗟𝗜𝗦𝗧-03-24-2`
- Removed the duplicate Share control from the WebApp.
- Added/kept the Other Lectures section in the start presentation using the
  same exact lecture URL.

### UI

- Kept the existing Team Leader look, typography, spacing, channel cards, and
  start-section styling.
- The protected WebApp now shows one clear Open Link action after the bounded
  ad flow instead of exposing a separate “Watch Ad” action.
- No unrelated redesign, referral, wallet, payment, spin, or giveaway feature
  was added.