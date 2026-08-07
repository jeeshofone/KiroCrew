---
name: web-browse
description: Render a REAL external web page in KiroCrew's BUILT-IN Browser panel (the right-side embedded Chromium view) with browser_navigate. Use when the user wants to VIEW / verify / "show me" an actual website or public URL (not a local dev server — that's the web-preview skill). View-only and ungated; operating the page (clicking, typing, multi-step) requires the Globe toggle ("Let the agent use the browser").
triggers: open this page, show me this site, show me the page, view this url, render this page, look at this website, open in the browser, see what this page looks like, pull up this site, visit this url
---

# Web Browse — render a real page in the Browser panel

KiroCrew's chat right-side **Browser** panel is a real embedded Chromium view.
When the user wants to *see* an actual external web page (a public site, a docs
page, a page they just deployed), open it with `browser_navigate` — the page
loads in the **built-in browser** and the panel surfaces itself automatically.

This is the **view** path. It is deliberately narrow: open the URL and show it,
nothing more. It does NOT require the user to turn on the Globe toggle — the user
asking for a page IS the consent, exactly as if they had typed the URL into the
panel themselves.

## How the panel works (so you set expectations correctly)

The panel is normally a **native `WebContentsView`** owned by the Electron main
process and composited over the panel's rectangle: native paint, real events,
downloads, video. The user can click and type in it directly at any time — their
own input is never gated.

Two things follow from that:

- **You do not need a screenshot to make the page appear.** `browser_navigate`
  alone opens the built-in browser and the dashboard reveals the panel. Take a
  screenshot only when *you* need to look at the page.
- **A screenshot is not what the user sees.** They are watching the live view.

**Playwright is the FALLBACK, not the default.** When no native view can serve
the session — a remote gateway, a non-Electron host — the same `browser_*` tools
transparently fall back to an out-of-process Playwright browser whose frames are
streamed into the panel as a read-only mirror. That mirror is a degraded mode: no
real input channel, just painted frames. If you find yourself on it locally, that
is a bug worth reporting, not the intended path.

## What the Globe toggle actually governs

The Globe ("let the agent act") authorizes **you** to *operate* the page. It has
nothing to do with whether the built-in browser is used, and it never gates the
user's own clicking and typing in the panel.

| Class | Ops | Globe needed? |
|---|---|---|
| **View** | `browser_navigate`, `browser_navigate_back`, `browser_snapshot`, `browser_take_screenshot`, `browser_console_messages`, `browser_wait_for` | No |
| **Operate** | `browser_click`, `browser_type`, `browser_press_key`, `browser_hover`, `browser_select_option`, `browser_evaluate` | **Yes** |

An operate-class call with the Globe off is refused with
`agent-act-not-authorized`. That is an authorization answer, not a transport
problem — do not retry it or route around it. Tell the user to flip the **Globe**
on (the Browser panel also has a "Let the agent act" button), then drive it.

## Precondition — Playwright must be available (the guard)

The `browser_*` tool NAMES still come from the external `@playwright/mcp`
package, even when the ops are served natively, so it must be present for the
tools to exist at all.

- If the `browser_*` tools are **not** in your tool list, do NOT attempt this.
  Fall back to `web_fetch` to read the page, and tell the user:
  > "The built-in browser isn't set up. Run `kirocrew browse setup` — it writes
  >  the config, registers the proxy, and tells you if `@playwright/mcp` needs
  >  installing (`npm i -g @playwright/mcp`). Then restart the gateway
  >  (`kirocrew stop && kirocrew gateway`). For now, here's what I read from the
  >  page."
- Only proceed with the steps below when the `browser_*` tools are present.

## Steps

1. Confirm the URL is a valid, real `http(s)://` page (you can find/derive it
   from the conversation — you don't need the user to paste it). Only `http` and
   `https` are accepted; `file:`, `data:` and `javascript:` are refused by the
   same guard the user's own panel controls go through.
2. `browser_navigate` to it (use `waitUntil: "domcontentloaded"` for SPAs).
3. Tell the user it's showing in the Browser panel, in one line.
4. Screenshot only if *you* need to inspect the rendering yourself.

## View vs. operate

- **View** (this skill): open a URL and show it. No Globe toggle needed.
- **Operate** (click, type, fill forms, multi-step navigation): that's the
  Globe toggle ("Let the agent use the browser") / `[BROWSE]` mode — it authorizes you to actively
  drive the browser across turns. If the user asks you to *interact* with a page
  that's only being viewed (Globe off), don't silently start operating: tell
  them to flip the **Globe** on (the panel also has a "Let the agent act"
  button that turns it on), then drive it.

## Not this skill

- **Local dev / static server** (localhost, a site the user is building) →
  that's the `web-preview` skill (a loopback iframe), not Playwright. If you are
  checking a front-end change **you** just made on a loopback URL, that's the
  `web-verify` skill (navigate + screenshot + read the frame).
- **Just reading text** with no need to show the page → `web_fetch` is cheaper;
  only use the browser when the user wants to *see* the rendered page.
