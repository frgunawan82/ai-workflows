---
applies_when: Any Playwright browser session — choosing headed vs headless and whether to load/save the `.auth/` profile. Page actions, selectors and waits are out of scope.
---

## Rules

- **Ops task ⇒ headed, real Chrome.** An ops task is operational work on a live system or account — production verification, provisioning, configuring a console (cloud, DNS, OAuth, SaaS admin, hosting panel), creating/editing/deleting records, approving, paying, inviting, deploying, bulk clean-up — and any step that signs in. Start it with `playwright_launch.py --ensure --headed --channel chrome --port 17337`, shutting down a running headless daemon first as the `playwright` workflow describes. Reason: the user must be able to watch a state change land and step in for 2FA, a captcha, or a wrong-target click, and many providers refuse sign-in from bundled Chromium.
- **Read-only browsing stays headless** — open a URL, read, scrape, screenshot, check a status page — unless it lands on a sign-in screen (switch to headed) or the user asks for headed. Reason: headless is faster and does not take over the screen.
- **Load a saved profile before navigating.** `list_auth`, then `load_auth --param name=<profile>` when a matching profile exists; `load_auth` restarts the browser context and drops open pages, so it goes first. Then navigate and confirm the page is not a sign-in or account-chooser screen before trusting it. Reason: a loaded profile is not a live session — cookies expire silently, and a saved profile nobody loads is wasted.
- **Save auth by default after any login.** Once the target page shows a signed-in state, run `save_auth --param name=<profile>` without asking — unless the user opted out for this task ("don't save", "don't remember", "one-off", "incognito"). Opt-out means no `save_auth`, no copying cookies anywhere else, and the reply says it was skipped. Reason: the user chose this default so the next run reuses the session headlessly instead of costing another human login; the opt-out is the only exception.
- **One profile per service or account, kebab-case, refreshed in place.** Name it `<service>` or `<service>-<account>` (for example `github`, `google-work`); after a re-login save over the same name — never `-new` / `-2` variants. Reason: `load_auth` needs a predictable name and duplicates hide which profile is live.
- **`chmod 600` every profile right after `save_auth`.** `storage_state()` writes with the umask default, so a fresh profile is world-readable until fixed. Reason: the file *is* the session cookie — handle it like a password.
- **Never persist the credentials themselves.** Cookies and storage go in `$HOME/.agents/workflows/playwright/.auth/` only; passwords, OTP codes and recovery keys are typed by the user in the headed window and never written to a file, a script, or the vault. Reason: the vault hard rule is no secrets, and `.auth/` is never committed.
- **Report the auth decision in the reply.** State which profile was loaded, saved, refreshed, or deliberately not saved. Reason: the user cannot see the daemon; the reply is the only audit trail.

## Verify

```bash
PY="$HOME/.agents/.venv/bin/python"; WF="$HOME/.agents/workflows/playwright"

# Daemon mode matches the task: ops task → "headless": false and "channel": "chrome"
"$PY" "$WF/playwright_client.py" --port 17337 health

# The profile you reported exists (or is deliberately absent after an opt-out)
"$PY" "$WF/playwright_client.py" --port 17337 action list_auth

# Every profile is owner-only (expect no output)
find "$WF/.auth" -name '*.json' ! -perm 600 -print
```
