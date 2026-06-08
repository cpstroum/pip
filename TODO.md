# Nemma — Outstanding Tasks

## Reminder: always upload `.env` to the device

The `.env` file is not in the git repo (intentionally — it has API keys).
After any fresh copy to the Unihiker, upload `.env` separately:

```bash
scp .env root@10.1.2.3:/root/pip/
```

## Nice to haves

- Try additional `gpt-realtime-2` voices for Nemma (`sage`, `shimmer`, `marin`)
- Add a long-press or inactivity timeout to return to the profile picker
- Consider whether the conversation loop should persist across sessions or reset
