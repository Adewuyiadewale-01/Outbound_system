"""Browser automation for the engagement workflow: CDP plumbing, JS payloads,
reactor collection, profile inspection, and candidate assessment."""

from __future__ import annotations

import json
import os
import random
import time
from typing import Any
from urllib.parse import urlsplit

from outbound.engagement.assessment import (
    ACTIVITY_ASSESSMENT_VERSION,
    PROFILE_PARSER_VERSION,
    activity_counts,
    qualifies,
    recommendation_for,
    validate_profile_gate,
)
from outbound.engagement.batches import choose_like_target
from outbound.engagement.campaign import save_campaign
from outbound.engagement.config import CDP_ACCOUNTS
from outbound.engagement.parsing import (
    canonical_profile_url,
    classify_location,
    parse_follower_count,
    parse_relative_age_hours,
)
from outbound.engagement.paths import HIGH_SIGNAL_PATH, HISTORY_PATH, STATE_DIR
from outbound.shared.diversion import (
    generate_lead_diversion_plan,
    generate_lead_diversion_seconds_plan,
)
from outbound.shared.state import now, read_json, write_json


def execution_event(campaign, candidate=None, *, action, method=None, reason=None):
    previous = campaign.get("execution", {})
    changed = candidate is not None and previous.get("profile_url") != candidate.get("profile_url")
    campaign["execution"] = {
        "profile_name": (candidate or {}).get("name")
        or ("LinkedIn member" if candidate else previous.get("profile_name", "")),
        "profile_url": (candidate or {}).get("profile_url", previous.get("profile_url", "")),
        "action": action,
        "navigation_method": method
        if method is not None
        else ("" if changed else previous.get("navigation_method", "")),
        "fallback_reason": reason
        if reason is not None
        else ("" if changed else previous.get("fallback_reason", "")),
        "updated_at": now().isoformat(),
    }
    save_campaign(campaign)


def append_history(value: dict[str, Any]) -> None:
    path = (
        STATE_DIR / "navigation.jsonl"
        if value.get("type") in {"navigation", "navigation_failed"}
        else HISTORY_PATH
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def ensure_obf_diversions(campaign: dict[str, Any], config: dict[str, Any]) -> None:
    """Assign OBF's persisted diversion plan to this campaign's profiles."""
    if not bool(config.get("obf_style_diversions", True)):
        return
    candidates = [
        candidate for candidate in campaign.get("candidates", []) if candidate.get("profile_url")
    ]
    for slot_id, candidate in enumerate(candidates, start=1):
        candidate.setdefault("diversion_slot", slot_id)
    missing = [candidate for candidate in candidates if not candidate.get("obf_diversion")]
    if not missing:
        return

    slots = [int(candidate["diversion_slot"]) for candidate in candidates]
    kinds = {
        int(entry["slot_id"]): entry["lead_diversion"]
        for entry in generate_lead_diversion_plan(campaign["day"], slots)["entries"]
    }
    seconds = {
        int(entry["slot_id"]): entry.get("lead_diversion_sec")
        for entry in generate_lead_diversion_seconds_plan(
            campaign["day"],
            [{"slot_id": slot, "lead_diversion": kinds[slot]} for slot in slots],
        )["entries"]
    }
    for candidate in candidates:
        slot = int(candidate["diversion_slot"])
        candidate.update(
            obf_diversion=kinds[slot],
            obf_diversion_seconds=seconds[slot],
            diversion_plan_version="obf-v1",
        )


def run_obf_diversion(session: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    """Execute the same diversion primitives OBF uses, without changing outcome."""
    from outbound.shared.diversion import _run_diversion

    activity = {
        "recent_items": [
            {"post_url": post.get("post_url", "")} for post in candidate.get("posts", [])
        ]
    }
    result = _run_diversion(
        session,
        str(candidate.get("obf_diversion") or "none"),
        candidate.get("obf_diversion_seconds"),
        activity=activity,
        company_linkedin=str(candidate.get("company_linkedin") or ""),
    )
    candidate["diversion_result"] = result
    candidate["diversion_completed_at"] = now().isoformat()
    return result


def _evaluate_json(cdp: Any, expression: str, timeout: float = 30) -> Any:
    raw = cdp.evaluate(expression, await_promise=True, timeout=timeout)
    return json.loads(raw) if isinstance(raw, str) else raw


SOURCE_REACTOR_BOOTSTRAP_JS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const roots = []; const add = root => { roots.push(root); root.querySelectorAll?.('*').forEach(el => { if (el.shadowRoot) add(el.shadowRoot); }); }; add(document);
  const all = sel => roots.flatMap(root => Array.from(root.querySelectorAll?.(sel) || []));
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  let dialog = all('dialog,[role="dialog"]').filter(visible).sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height)[0];
  let trigger = null;
  if (!dialog) {
    // 1. MAIN METHOD: Structural
    const allEls = all('*');
    const commentsEl = allEls.find(el => {
      const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      return /^\d+\s+comments?/.test(t) && el.children.length === 0;
    });
    if (commentsEl) {
      let container = commentsEl;
      const commentsRect = commentsEl.getBoundingClientRect();
      for (let i = 0; i < 20; i++) {
        container = container.parentElement;
        if (!container) break;
        const clickables = Array.from(container.querySelectorAll('a, button, [role="button"]'))
          .filter(visible)
          .filter(el => {
            if (el.contains(commentsEl)) return false;
            if (!(el.compareDocumentPosition(commentsEl) & Node.DOCUMENT_POSITION_FOLLOWING)) return false;
            return Math.abs(el.getBoundingClientRect().top - commentsRect.top) < 20;
          });
        if (clickables.length > 0) { trigger = { el: clickables[0] }; break; }
      }
    }
    // 2. FALLBACK 1: Exact LinkedIn DOM selectors
    if (!trigger) {
      const exactSelectors = ['button.social-details-social-counts__reactions-count', 'button.social-details-social-counts__count-value', 'li.social-details-social-counts__reactions button', 'li.social-details-social-counts__reactions a'];
      for (const sel of exactSelectors) {
        const found = all(sel).filter(visible);
        if (found.length > 0) { trigger = { el: found[0] }; break; }
      }
    }
    // 3. FALLBACK 2: Original Text Regex (Updated to include aria-label)
    if (!trigger) {
      const reactionCount = (text, aria) => { 
        const full = (text + ' ' + (aria || '')).trim();
        const others = full.match(/\b([\d,]+)\s+others?\s+reacted\b/i); 
        if (others) return parseInt(others[1].replace(/,/g,'')) + 1; 
        const direct = full.match(/\b([\d,]+)\s+reactions?\b/i); 
        return direct ? parseInt(direct[1].replace(/,/g,'')) : 0; 
      };
      const candidates = all('a,button,[role="button"]').filter(visible).map(el => ({el, text: (el.innerText || el.textContent || '').replace(/\s+/g,' ').trim(), aria: el.getAttribute('aria-label') || ''})).filter(x => reactionCount(x.text, x.aria) > 0);
      candidates.sort((a,b) => reactionCount(b.text, b.aria) - reactionCount(a.text, a.aria));
      if (candidates.length > 0) trigger = { el: candidates[0].el };
    }
    if (!trigger) return JSON.stringify({success:false,error:'reaction_trigger_not_found'});
    trigger.el.click(); await sleep(1200);
    for(let waitPass=0; waitPass<30 && !dialog; waitPass++) { roots.splice(0); add(document); dialog = all('dialog,[role="dialog"]').filter(visible).sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height)[0]; if(!dialog) await sleep(300); }
  }
  if (!dialog) return JSON.stringify({success:false,error:'reactions_dialog_not_found'});
  // LinkedIn mounts the dialog shell first and hydrates the reaction rows a
  // moment later. Wait for the count/rows before handing control to Python.
  for (let waitPass=0; waitPass<20; waitPass++) {
    roots.splice(0); add(document);
    const liveDialogs = all('dialog,[role="dialog"]').filter(visible).sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height);
    if (liveDialogs[0]) dialog = liveDialogs[0];
    const liveRoots=[]; const liveWalk=root=>{liveRoots.push(root);if(root.shadowRoot)liveWalk(root.shadowRoot);root.querySelectorAll?.('*').forEach(el=>{if(el.shadowRoot)liveWalk(el.shadowRoot);});}; liveWalk(document);
    const liveAll=sel=>liveRoots.flatMap(root=>Array.from(root.querySelectorAll?.(sel)||[]));
    const liveText=liveAll('button,[role="button"],h2').map(el=>(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()).find(t=>/\bAll\s+[\d,]+/i.test(t)||/^[\d,]+\s+All/i.test(t))||'';
    if (/\bAll\s+[\d,]+/i.test(liveText) || /^[\d,]+\s+All/i.test(liveText)) break;
    const refreshVisible = liveAll('button,[role="button"]').some(el => /\brefresh\b/i.test((el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()));
    if (refreshVisible) return JSON.stringify({success:false,error:'reactions_modal_refresh_required'});
    await sleep(300);
  }
  const dr = dialog.getBoundingClientRect();
  const scoped=[]; const walk=root=>{ scoped.push(root); if(root.shadowRoot) walk(root.shadowRoot); root.querySelectorAll?.('*').forEach(el=>{if(el.shadowRoot)walk(el.shadowRoot);}); }; walk(dialog);
  const inDialog = el => { const r=el.getBoundingClientRect(); const cx=r.left+r.width/2, cy=r.top+r.height/2; return r.width>0&&r.height>0&&cx>=dr.left&&cx<=dr.right&&cy>=dr.top&&cy<=dr.bottom; };
  const dialogAll = sel => scoped.flatMap(root => Array.from(root.querySelectorAll?.(sel)||[])).filter(inDialog);
  if (dialogAll('button,[role="button"]').some(el => /\brefresh\b/i.test((el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()))) return JSON.stringify({success:false,error:'reactions_modal_refresh_required'});
  const countText = dialogAll('button,[role="button"],h2').map(el=>(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()).find(t=>/\bAll\s+[\d,]+/i.test(t)||/^[\d,]+\s+All/i.test(t)) || '';
  const expected = parseInt((countText.match(/[\d,]+/)||['0'])[0].replace(/,/g,''));
  let sourceCard=trigger?.el; for(let i=0;i<10&&sourceCard;i++,sourceCard=sourceCard.parentElement){if(sourceCard.matches?.('[data-view-name="feed-full-update"],article[data-urn]'))break;}
  const sourceTimestamp=(sourceCard?.querySelector('.update-components-actor__sub-description,.feed-shared-actor__sub-description')?.innerText||'').trim();
  return JSON.stringify({success:true,resolved_url:location.href,source_timestamp:sourceTimestamp,expected,dialog_ready:true});
})()
"""

SOURCE_REACTOR_SNAPSHOT_JS = r"""
(() => {
  const roots=[]; const add=root=>{roots.push(root);root.querySelectorAll?.('*').forEach(el=>{if(el.shadowRoot)add(el.shadowRoot);});}; add(document);
  const visible=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const dialogs=roots.flatMap(r=>Array.from(r.querySelectorAll?.('dialog,[role="dialog"]')||[])).filter(visible).sort((a,b)=>b.getBoundingClientRect().height-a.getBoundingClientRect().height);
  const dialog=dialogs[0]; if(!dialog)return JSON.stringify({success:false,error:'reactions_dialog_not_found'});
  const dr=dialog.getBoundingClientRect();
  const scoped=[]; const walk=root=>{scoped.push(root);if(root.shadowRoot)walk(root.shadowRoot);root.querySelectorAll?.('*').forEach(el=>{if(el.shadowRoot)walk(el.shadowRoot);});}; walk(dialog);
  const inDialog=el=>{const r=el.getBoundingClientRect();const cx=r.left+r.width/2,cy=r.top+r.height/2;return r.width>0&&r.height>0&&cx>=dr.left&&cx<=dr.right&&cy>=dr.top&&cy<=dr.bottom;};
  const all=sel=>scoped.flatMap(r=>Array.from(r.querySelectorAll?.(sel)||[]));
  if(all('button,[role="button"]').some(el=>/\brefresh\b/i.test((el.innerText||el.textContent||'').replace(/\s+/g,' ').trim())))return JSON.stringify({success:false,error:'reactions_modal_refresh_required'});
  const found=new Map();
  all('a[href*="linkedin.com/in/"],a[href^="/in/"]').filter(visible).forEach(a=>{const href=a.href||a.getAttribute('href');if(href){const url=href.split('?')[0];found.set(url,{url,name:(a.innerText||a.textContent||'').replace(/\s+/g,' ').trim()});}});
  const text=all('button,[role="button"],h2').map(el=>(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim()).find(t=>/\bAll\s+[\d,]+/i.test(t)||/^[\d,]+\s+All/i.test(t))||'';
  const expected=parseInt((text.match(/[\d,]+/)||['0'])[0].replace(/,/g,''));
  const divs=all('div').filter(el=>{const s=getComputedStyle(el);return /(auto|scroll)/.test(s.overflowY)&&el.scrollHeight>el.clientHeight+20;});
  const scroller=divs.sort((a,b)=>(b.scrollHeight-b.clientHeight)-(a.scrollHeight-a.clientHeight))[0];
  const sr=scroller?.getBoundingClientRect();
  return JSON.stringify({success:true,expected,profiles:Array.from(found.values()),profiles_collected:found.size,scroller:scroller&&sr?{left:sr.left,top:sr.top,width:sr.width,height:sr.height,scrollTop:scroller.scrollTop,scrollHeight:scroller.scrollHeight,clientHeight:scroller.clientHeight}:null});
})()
"""


PROFILE_GATE_JS = r"""
(() => {
  // Keep each field in a deliberately bounded area. The header owns identity
  // data; Activity is the permitted follower-count fallback.
  const main=document.querySelector('main');
  const text=e=>(e?.innerText||e?.textContent||'').replace(/\s+/g,' ').trim();
  const vis=e=>{if(!e)return false;const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  if(!main)return JSON.stringify({error:'profile_main_missing',page_url:location.href});
  const contact=Array.from(main.querySelectorAll('a[href*="/overlay/contact-info/"]')).find(vis);
  if(!contact)return JSON.stringify({error:'profile_contact_info_missing',page_url:location.href});
  const contactP=contact.closest('p');
  const metadataRow=contactP?.parentElement;
  const locationEl=Array.from(metadataRow?.children||[]).find(e=>e.matches?.('p')&&e!==contactP&&vis(e)&&text(e));
  const actionPattern=/\b(follow|connect|message|more|save in sales navigator|visit my website|book an appointment)\b/i;
  const actions=root=>Array.from(root?.querySelectorAll('button,a,[role="button"]')||[]).filter(e=>vis(e)&&actionPattern.test(`${text(e)} ${e.getAttribute('aria-label')||''}`));
  const headings=root=>Array.from(root?.querySelectorAll('h1,h2')||[]).filter(e=>vis(e)&&text(e)&&!/\bnotifications?\b/i.test(text(e)));
  const followerMatches=root=>Array.from(root?.querySelectorAll('p,span')||[]).filter(e=>vis(e)&&/\bfollowers?\b/i.test(text(e))).sort((a,b)=>text(a).length-text(b).length);

  // Contact info belongs to the profile header section. Search no farther
  // than that section when deriving the smaller card shown in the UI.
  const headerSection=contact.closest('section');
  if(!headerSection||!main.contains(headerSection))return JSON.stringify({error:'profile_header_section_missing',page_url:location.href});
  let headerCard=metadataRow;
  while(headerCard){
    if(headings(headerCard).length&&actions(headerCard).length)break;
    if(headerCard===headerSection){headerCard=null;break;}
    headerCard=headerCard.parentElement;
  }
  if(!headerCard)return JSON.stringify({error:'profile_header_missing',page_url:location.href});
  const nameEl=headings(headerCard)[0];
  const headerFollower=followerMatches(headerCard)[0];

  // Some profiles omit followers from the header. Permit the explicit
  // Activity section only, excluding follower text inside an Activity post.
  const activityHeading=Array.from(main.querySelectorAll('h1,h2,h3,[role="heading"]')).find(e=>vis(e)&&text(e)==='Activity'&&e.closest('section')&&e.closest('section')!==headerSection);
  const activitySection=activityHeading?.closest('section');
  const activityFollower=Array.from(activitySection?.querySelectorAll('p,span')||[])
    .filter(e=>vis(e)&&/\bfollowers?\b/i.test(text(e))&&!e.closest('[data-view-name="feed-full-update"],article,[data-urn]'))
    .sort((a,b)=>a.getBoundingClientRect().top-b.getBoundingClientRect().top||text(a).length-text(b).length)[0];
  const followerEl=headerFollower||activityFollower;
  const followerSource=headerFollower?'header':activityFollower?'activity':'missing';
  return JSON.stringify({
    name:text(nameEl),
    location:text(locationEl),
    follower_text:text(followerEl),
    follower_source:followerSource,
    contact_info_text:text(contact),
    page_url:location.href
  });
})()
"""


POST_CARDS_JS = r"""
(() => JSON.stringify(Array.from(document.querySelectorAll('[data-view-name="feed-full-update"]')).map(card => {
  const scope=card.getAttribute('data-view-tracking-scope')||''; const urn=(scope.match(/urn:li:activity:\d+/)||[''])[0];
  const timeEl=card.querySelector('.update-components-actor__sub-description'); const like=card.querySelector('button[aria-label="React Like"]');
  const author=card.querySelector('a[href*="/in/"]');
  return {post_urn:urn,post_url:urn?'https://www.linkedin.com/feed/update/'+urn+'/':'',timestamp:(timeEl?.innerText||'').trim(),liked:like?.getAttribute('aria-pressed')==='true',like_available:!!like,author_url:(author?.href||'').split('?')[0]};
}).filter(x=>x.post_urn)))()
"""


def _connect_campaign_browser(
    campaign: dict[str, Any], config: dict[str, Any], execute: bool
) -> Any:
    from linkedin_helper import HumanSimulator, LinkedInSession, inject_stealth

    endpoint = CDP_ACCOUNTS[config["cdp_account"]]
    os.environ["LINKEDIN_CDP_HOST"] = endpoint["host"]
    os.environ["LINKEDIN_CDP_PORT"] = str(endpoint["port"])
    session = LinkedInSession()
    preflight = session.connect(skip_rate_check=not execute)
    if not preflight.get("ok"):
        raise RuntimeError(preflight.get("block_reason") or "LinkedIn preflight failed")
    cdp = session.cdp
    target = cdp.create_page_target("https://www.linkedin.com/feed/")
    inject_stealth(cdp)
    campaign["target_id"] = target["target_id"]
    campaign["cdp_account"] = config["cdp_account"]
    campaign["stage"] = "browser_ready"
    save_campaign(campaign)
    simulator = HumanSimulator(cdp)
    session.sim = simulator
    return cdp, simulator, session


def _navigate(cdp: Any, url: str, settle: float | None = None) -> None:
    """Use the shared readiness checks; deadlines are ceilings, not sleeps."""
    from linkedin_helper import _wait_for_activity_feed_state, _wait_for_linkedin_ready

    activity = "/recent-activity/" in url
    selector = (
        '[data-view-name="feed-full-update"], .feed-shared-update-v2, main'
        if activity
        else 'main h1, main h2, a[href*="/overlay/contact-info/"], main'
    )
    events = []
    deadline = time.monotonic() + 180
    for attempt in range(3):
        event = {"url": url, "attempt": attempt + 1, "started_at": now().isoformat()}
        events.append(event)
        try:
            try:
                result = cdp.navigate(url, wait_load=False, timeout=8)
                if result and result.get("errorText"):
                    raise RuntimeError(result["errorText"])
            except (TimeoutError, RuntimeError) as error:
                event["navigation_error"] = str(error)
                # A timed-out command may already have navigated successfully.
                current = str(cdp.evaluate("location.href", timeout=5) or "")
                if urlsplit(current).path.rstrip("/") != urlsplit(url).path.rstrip("/"):
                    cdp.evaluate("window.location.href = " + json.dumps(url), timeout=8)
                    event["fallback"] = "js_location"
            arrived = False
            until = min(deadline, time.monotonic() + 20)
            while time.monotonic() < until:
                current = str(cdp.evaluate("location.href", timeout=5) or "")
                if current.startswith("chrome-error:"):
                    raise RuntimeError("browser_network_error_page")
                if any(
                    part in urlsplit(current).path
                    for part in ("/checkpoint/", "/login", "/authwall")
                ):
                    raise PermissionError("LinkedIn authentication/checkpoint requires attention")
                requested = urlsplit(url)
                arrived_via_short_link = (
                    (requested.hostname or "").endswith("lnkd.in")
                    and (urlsplit(current).hostname or "").endswith("linkedin.com")
                    and urlsplit(current).path.startswith("/posts/")
                )
                if arrived_via_short_link or (
                    urlsplit(current).hostname == requested.hostname
                    and urlsplit(current).path.rstrip("/") == requested.path.rstrip("/")
                ):
                    arrived = True
                    break
                time.sleep(0.35)
            if not arrived:
                raise RuntimeError("requested_destination_not_reached")
            ready = _wait_for_linkedin_ready(
                cdp,
                expected_selector=selector,
                timeout=min(45, max(1, deadline - time.monotonic())),
                stable_for=0.5,
                ignored_overlays={".authentication-outlet"},
                ignored_loaders={".artdeco-loader", '[aria-busy="true"]'},
            )
            event["readiness"] = ready
            if not ready.get("ready"):
                raise RuntimeError("page_not_ready")
            if activity:
                feed = _wait_for_activity_feed_state(
                    cdp, timeout=min(45, max(1, deadline - time.monotonic()))
                )
                event["feed_readiness"] = feed
                if not feed.get("ready"):
                    raise RuntimeError("activity_feed_not_hydrated")
            event["ok"] = True
            cdp.post_engagement_navigation = events
            append_history({"type": "navigation", "events": events, "at": now().isoformat()})
            return
        except PermissionError:
            raise
        except Exception as error:
            event["error"] = str(error)
            if attempt == 2 or time.monotonic() >= deadline:
                break
            try:
                cdp.evaluate("document.readyState", timeout=5)
                cdp.send("Page.stopLoading", timeout=5)
                event["recovery"] = "stop_loading_and_retry"
            except Exception:
                # Preserve every other workflow's tab and stay on this account.
                target = cdp.create_page_target("about:blank")
                event["recovery"] = "fresh_workflow_tab"
                event["target_id"] = target.get("target_id")
    cdp.post_engagement_navigation = events
    append_history({"type": "navigation_failed", "events": events, "at": now().isoformat()})
    raise RuntimeError("Navigation failed after recovery: " + str(events[-1].get("error")))


def collect_sources(cdp: Any, campaign: dict[str, Any], config: dict[str, Any]) -> None:
    known = {c.get("profile_url") for c in campaign["candidates"]}
    for source in campaign["sources"]:
        if source.get("status") == "collected" and source.get("stop_reason") in {
            "exhausted",
            "stagnant",
        }:
            continue
        # A visible Refresh control is an explicitly detected transient loading
        # failure, not evidence that the source has no reactors. Reload the
        # source and reopen the modal a bounded number of times.
        result: dict[str, Any] = {}
        collection_attempt = 0
        max_collection_attempts = int(config.get("source_collection_max_attempts", 3))
        while collection_attempt < max_collection_attempts:
            collection_attempt += 1
            _navigate(cdp, source["submitted_url"])
            result = _evaluate_json(cdp, SOURCE_REACTOR_BOOTSTRAP_JS, timeout=90)
            if result.get("success") or result.get("error") != "reactions_modal_refresh_required":
                break
            source.update(
                status="retrying_network",
                collection_attempts=collection_attempt,
                last_retry_reason="reactions_modal_refresh_required",
            )
            save_campaign(campaign)
            if collection_attempt < max_collection_attempts:
                time.sleep(random.uniform(2.0, 4.0))
        source["collection_attempts"] = collection_attempt
        if not result.get("success"):
            source.update(status="failed", error=result.get("error", "source_collection_failed"))
            save_campaign(campaign)
            continue
        expected = int(result.get("expected") or 0)
        collection_cap = int(config.get("source_collection_cap", 200))
        effective_target = min(expected, collection_cap) if expected else collection_cap
        found: dict[str, dict[str, Any]] = {}
        stagnant = 0
        passes = 0
        retry_rounds = 0
        max_retry_rounds = 3
        stop_reason = "pass_limit"
        collection_error = ""
        while passes < 160:
            passes += 1
            snapshot = _evaluate_json(cdp, SOURCE_REACTOR_SNAPSHOT_JS, timeout=12)
            if not snapshot.get("success"):
                collection_error = snapshot.get("error", "reactions_dialog_not_found")
                stop_reason = "collection_error"
                break
            before = len(found)
            for profile in snapshot.get("profiles", []):
                url = str(profile.get("url") or "").split("?")[0]
                if url:
                    found[url] = {"url": url, "name": profile.get("name", "")}
            stagnant = stagnant + 1 if len(found) == before else 0
            expected = expected or int(snapshot.get("expected") or 0)
            effective_target = min(expected, collection_cap) if expected else collection_cap
            if len(found) >= effective_target:
                stop_reason = "cap_reached" if expected > collection_cap else "exhausted"
                break
            # Stagnation handling: pause and retry instead of giving up
            if stagnant >= 5:
                retry_rounds += 1
                if retry_rounds >= max_retry_rounds:
                    stop_reason = "stagnant"
                    break
                # Back off for 15-25 seconds, then resume scrolling
                time.sleep(random.uniform(15, 25))
                stagnant = 0
                continue
            rect = snapshot.get("scroller") or {}
            # The modal shell can report a valid dialog for a few hundred ms
            # before LinkedIn mounts its virtualized scroll container. Do not
            # treat that hydration window as a completed/failed extraction.
            if not rect:
                time.sleep(random.uniform(0.45, 0.9))
                continue
            x = float(rect.get("left", 0)) + float(rect.get("width", 0)) / 2
            y = float(rect.get("top", 0)) + float(rect.get("height", 0)) / 2
            try:
                cdp.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": x, "y": y},
                    timeout=8,
                )
                cdp.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": 0,
                        "deltaY": max(100, int(float(rect.get("height", 495)) * 0.75)),
                    },
                    timeout=8,
                )
            except Exception as error:
                collection_error = str(error)
                stop_reason = "scroll_error"
                break
            time.sleep(min(3, 0.7 + stagnant * 0.5))
        if stagnant >= 5 and stop_reason == "pass_limit":
            stop_reason = "stagnant"
        coverage = len(found) / effective_target if effective_target else 0
        source.update(
            stop_reason=stop_reason,
            extraction_error=collection_error,
            passes=passes,
            stagnant_passes=stagnant,
            retry_rounds=retry_rounds,
        )
        result.update(
            profiles=list(found.values()),
            profiles_collected=len(found),
            coverage=coverage,
            expected=expected,
            effective_target=effective_target,
            passes=passes,
            stagnant_passes=stagnant,
            retry_rounds=retry_rounds,
        )
        try:
            _evaluate_json(
                cdp,
                "(() => { const b=Array.from(document.querySelectorAll('button')).find(x=>/dismiss/i.test(x.getAttribute('aria-label')||'')); if(b)b.click(); return true; })()",
                timeout=8,
            )
        except Exception:
            pass
        # The error can surface after the modal initially opened, while its
        # virtualized rows are loading. Use the remaining bounded attempts for
        # the same source before recording a partial result.
        if (
            collection_error == "reactions_modal_refresh_required"
            and collection_attempt < max_collection_attempts
        ):
            source.update(
                status="retrying_network",
                collection_attempts=collection_attempt,
                last_retry_reason="reactions_modal_refresh_required",
            )
            save_campaign(campaign)
            time.sleep(random.uniform(2.0, 4.0))
            return collect_sources(cdp, campaign, config)
        source_age = parse_relative_age_hours(result.get("source_timestamp", ""))
        if source_age is not None and source_age > int(config["source_max_age_days"]) * 24:
            source.update(
                status="rejected_too_old",
                source_timestamp=result.get("source_timestamp", ""),
                source_age_hours=source_age,
            )
            save_campaign(campaign)
            continue
        coverage = round(float(result.get("coverage", 0)), 4)
        is_collected = coverage >= float(config["reactor_min_coverage"])
        source.update(
            resolved_url=result.get("resolved_url", ""),
            source_timestamp=result.get("source_timestamp", ""),
            source_age_hours=source_age,
            reaction_count=result.get("expected", 0),
            profiles_collected=result.get("profiles_collected", 0),
            coverage=coverage,
            status="collected" if is_collected else "rejected_partial",
        )
        if is_collected:
            for profile in result.get("profiles", []):
                url = canonical_profile_url(profile.get("url", ""))
                if not url or url in known:
                    continue
                known.add(url)
                campaign["candidates"].append(
                    {
                        "profile_url": url,
                        "name": profile.get("name", ""),
                        "source_post": source.get("resolved_url") or source["submitted_url"],
                        "status": "discovered",
                        "attempts": 0,
                        "likes_assigned": choose_like_target(campaign["day"], url, config),
                        "likes_completed": 0,
                    }
                )
        save_campaign(campaign)


def review_recent_activity(cdp: Any, simulator: Any) -> dict[str, int]:
    """Briefly review the recent-activity list before acting on its posts."""
    metrics_js = """(() => { const card=document.querySelector('[data-view-name=\"feed-full-update\"]'); let node=card; while(node){const s=getComputedStyle(node);if(node.scrollHeight>node.clientHeight+40&&/(auto|scroll)/.test(s.overflowY))break;node=node.parentElement;} node=node||document.scrollingElement; const r=node.getBoundingClientRect(); return JSON.stringify({left:r.left,top:r.top,width:r.width,height:r.height,scrollTop:node.scrollTop||0,scrollHeight:node.scrollHeight||0,clientHeight:node.clientHeight||0}); })()"""
    metrics = _evaluate_json(cdp, metrics_js, timeout=8)
    before = int(metrics.get("scrollTop", 0) or 0)
    x = float(metrics.get("left", 0)) + max(20, float(metrics.get("width", 0)) / 2)
    y = float(metrics.get("top", 0)) + min(
        max(40, float(metrics.get("height", 0)) / 2), max(40, float(metrics.get("height", 0)) - 30)
    )
    # The activity feed is often an internal scrolling region. Wheel events
    # target that visible region directly, then the current newest posts are
    # restored before collecting their IDs.
    for distance in (random.randint(260, 440), random.randint(140, 260)):
        cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}, timeout=8)
        cdp.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": 0, "deltaY": distance},
            timeout=8,
        )
        time.sleep(random.uniform(0.8, 1.5))
    furthest = _evaluate_json(cdp, metrics_js, timeout=8)
    cdp.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseWheel",
            "x": x,
            "y": y,
            "deltaX": 0,
            "deltaY": -max(int(furthest.get("scrollTop", 0) or 0), 1),
        },
        timeout=8,
    )
    time.sleep(random.uniform(0.5, 0.9))
    return {"before": before, "furthest": int(furthest.get("scrollTop", 0) or 0)}


def _human_scroll_to_activity_link(cdp: Any) -> None:
    """Scroll gradually to the 'Show all posts' link like a human reader.

    Real users generate scroll events with pauses on their way to a link
    2000+ px down the page; a single programmatic jump is a bot signature.
    Gradual scrolling also triggers the profile's lazy-loaded sections.
    """
    scroll_js = r"""(() => {
        const link = document.querySelector('a[aria-label="Show all posts"], a[href*="/recent-activity/"]');
        if (!link) return JSON.stringify({found: false});
        const rect = link.getBoundingClientRect();
        return JSON.stringify({found: true, targetY: Math.round(window.scrollY + rect.top - 200)});
    })()"""
    try:
        target = json.loads(cdp.evaluate(scroll_js, timeout=5))
    except Exception:
        target = {"found": False}
    if not target.get("found"):
        return
    current = 0
    goal = int(target.get("targetY", 0))
    while current < goal:
        step = random.randint(300, 600)
        current = min(current + step, goal)
        cdp.evaluate(f"window.scrollTo(0, {current})", timeout=5)
        time.sleep(random.uniform(0.4, 1.1))


def inspect_candidate(
    cdp: Any, simulator: Any, candidate: dict[str, Any], config: dict[str, Any]
) -> None:
    campaign = getattr(cdp, "engagement_campaign", None)
    if campaign is not None:
        execution_event(
            campaign, candidate, action="Opening profile", method="Direct URL", reason=""
        )
    _navigate(cdp, candidate["profile_url"])
    gate = _evaluate_json(cdp, PROFILE_GATE_JS)
    validate_profile_gate(gate)
    followers = parse_follower_count(gate.get("follower_text", ""))
    resolved_profile_url = (
        canonical_profile_url(gate.get("page_url") or "") or candidate["profile_url"]
    )
    candidate.update(
        gate,
        profile_url=resolved_profile_url,
        follower_count=followers,
        profile_parser_version=PROFILE_PARSER_VERSION,
        profile_assessed_at=now().isoformat(),
        **classify_location(gate.get("location", "")),
    )
    from linkedin_helper import (
        _open_profile_activity_from_profile,
        _wait_for_activity_destination,
        _wait_for_activity_feed_state,
    )

    try:
        if campaign is not None:
            execution_event(campaign, candidate, action="Opening activity", method="DOM", reason="")
        # Human-paced profile engagement, fully instrumented: dwell like a
        # reader, scroll gradually to the activity link (never click 2000px
        # down without scrolling), click, then re-navigate after arrival to
        # force top-frame rendering (LinkedIn's SPA click renders the feed
        # inside a preload frame; the top frame stays a shell).
        t_phase = time.time()
        time.sleep(random.uniform(3.0, 10.0))
        dwell_sec = round(time.time() - t_phase, 1)

        t_phase = time.time()
        _human_scroll_to_activity_link(cdp)
        scroll_sec = round(time.time() - t_phase, 1)

        t_phase = time.time()
        navigation = _open_profile_activity_from_profile(cdp)
        click_sec = round(time.time() - t_phase, 1)

        t_phase = time.time()
        destination = (
            _wait_for_activity_destination(cdp, candidate["profile_url"].rstrip("/"), "posts")
            if navigation.get("clicked")
            else {}
        )
        transit_sec = round(time.time() - t_phase, 1)
        candidate["navigation_timings"] = {
            "profile_dwell_sec": dwell_sec,
            "scroll_to_link_sec": scroll_sec,
            "click_sec": click_sec,
            "spa_transit_sec": transit_sec,
            "destination_transit_reported": destination.get("transit_sec"),
        }
        if destination.get("arrived"):
            t_phase = time.time()
            time.sleep(random.uniform(2.0, 7.0))
            _navigate(cdp, destination["url"])
            candidate["navigation_timings"]["renavigate_sec"] = round(time.time() - t_phase, 1)

        t_phase = time.time()
        feed = _wait_for_activity_feed_state(cdp, timeout=15) if destination.get("arrived") else {}
        if destination.get("arrived"):
            candidate["navigation_timings"]["feed_check_sec"] = round(time.time() - t_phase, 1)

        if destination.get("arrived") and feed.get("ready"):
            candidate["posts_navigation"] = {
                "via": "selector_based_renavigation",
                "feed_readiness": feed,
            }
            candidate["navigation_timings"]["total_navigation_sec"] = round(
                sum(
                    v
                    for v in candidate["navigation_timings"].values()
                    if isinstance(v, (int, float))
                ),
                1,
            )
            cdp.post_engagement_navigation = [{"feed_readiness": feed, "renavigated": True}]
        elif not destination.get("arrived"):
            # Late-arrival insurance: the click may have landed after the
            # destination wait expired. Check the actual page state before
            # falling back to direct URL navigation.
            feed = _wait_for_activity_feed_state(cdp, timeout=5)
            if feed.get("ready"):
                candidate["posts_navigation"] = {
                    "via": "selector_based_late_arrival",
                    "destination": destination,
                    "feed_readiness": feed,
                }
                cdp.post_engagement_navigation = [{"feed_readiness": feed, "late_arrival": True}]
            else:
                raise RuntimeError("Activity click did not reach a ready posts feed")
        else:
            raise RuntimeError("Activity click did not reach a ready posts feed")
    except (RuntimeError, TimeoutError) as error:
        if campaign is not None:
            execution_event(
                campaign,
                candidate,
                action="Opening activity",
                method="Direct URL · fallback",
                reason=str(error),
            )
        _navigate(cdp, candidate["profile_url"].rstrip("/") + "/recent-activity/all/")
        candidate["posts_navigation"] = {
            "via": "direct_url_fallback",
            "reason": str(error),
            "destination": locals().get("destination"),
            "navigation": locals().get("navigation"),
            "feed": locals().get("feed"),
        }
    candidate["activity_review"] = review_recent_activity(cdp, simulator)
    posts = _evaluate_json(cdp, POST_CARDS_JS)
    if not posts:
        checks = getattr(cdp, "post_engagement_navigation", [])
        feed = checks[-1].get("feed_readiness", {}) if checks else {}
        if feed.get("reason") != "explicit_empty_state":
            raise RuntimeError("post_parser_returned_no_cards_for_loaded_feed")
    for post in posts:
        post["age_hours"] = parse_relative_age_hours(post.get("timestamp", ""))
    eligible = [
        p
        for p in posts
        if p["age_hours"] is not None
        and p["age_hours"] <= int(config["activity_window_days"]) * 24
        and not p["liked"]
    ]
    newest = min((p["age_hours"] for p in posts if p["age_hours"] is not None), default=None)
    candidate.update(
        posts=posts,
        newest_post_age_hours=newest,
        engagement_eligible=newest is not None and newest <= int(config["newest_post_max_hours"]),
        available_unliked_posts=len(eligible),
        status="audited",
    )


def click_like(cdp: Any, urn: str) -> bool:
    expression = r"""(async () => {
      const urn = %s;
      const button = () => Array.from(document.querySelectorAll('[data-view-name="feed-full-update"]'))
        .find(c => (c.getAttribute('data-view-tracking-scope') || '').includes(urn))
        ?.querySelector('button[aria-label="React Like"]');
      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
      let b = button();
      if (!b) return {error:'like_button_missing'};
      if (b.getAttribute('aria-pressed') === 'true') return {already_liked:true};
      b.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'});
      // Give each post its own reading interval, then re-resolve the button
      // and verify visibility because the page may change during the wait.
      await sleep(5000 + Math.random() * 20000);
      b = button();
      if (!b) return {error:'like_button_disappeared'};
      const r = b.getBoundingClientRect(), style = getComputedStyle(b);
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      const top = document.elementFromPoint(x, y);
      if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight ||
          style.visibility === 'hidden' || style.display === 'none' || !top || !b.contains(top) ||
          b.disabled || b.getAttribute('aria-disabled') === 'true') return {error:'like_button_not_visible_or_obstructed'};
      if (b.getAttribute('aria-pressed') === 'true') return {already_liked:true};
      b.click();
      for (let i=0; i<20; i++) {
        await sleep(250);
        if (button()?.getAttribute('aria-pressed') === 'true') return {liked:true};
      }
      return {error:'like_result_unconfirmed'};
    })()""" % json.dumps(urn)
    # Allow the full 25-second reading interval plus reaction verification.
    result = cdp.evaluate(expression, await_promise=True, timeout=40)
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict) or result.get("error"):
        raise RuntimeError(
            (result or {}).get("error", "like_result_invalid")
            if isinstance(result, dict)
            else "like_result_invalid"
        )
    return bool(result.get("liked"))


def follow_current_profile(cdp: Any, name: str) -> bool:
    expression = (
        """(() => { const wanted=%s.toLowerCase(); const b=Array.from(document.querySelectorAll('button')).find(x=>{const s=(x.getAttribute('aria-label')||x.innerText||'').trim().toLowerCase();return s.startsWith('follow')&&(wanted===''||s.includes(wanted));}); if(!b)return false;b.click();return true;})()"""
        % json.dumps(name)
    )
    return bool(cdp.evaluate(expression, timeout=10))


def assess_engaged_candidate(
    session: Any, candidate: dict[str, Any], config: dict[str, Any]
) -> bool:
    """Perform the expensive activity review once, after Likes qualify a profile."""
    recommendation = candidate.get("recommendation") or {}
    if (
        candidate.get("activity_assessment_status") == "complete"
        and int(recommendation.get("assessment_version") or 0) == ACTIVITY_ASSESSMENT_VERSION
    ):
        return True
    candidate["activity_assessment_status"] = "running"
    candidate["activity_assessment_started_at"] = now().isoformat()
    candidate["activity_assessment_attempts"] = (
        int(candidate.get("activity_assessment_attempts", 0)) + 1
    )
    try:
        cdp = getattr(session, "cdp", None)
        campaign = getattr(cdp, "engagement_campaign", None) if cdp else None
        if isinstance(campaign, dict):
            cdp.execution_observer = lambda **event: execution_event(campaign, candidate, **event)
        detail = session.read_activity_detail(
            candidate["profile_url"],
            max_seconds=75,
            navigation_type="selector_based",
            tab_order=[("reactions", "Reactions"), ("comments", "Comments"), ("posts", "Posts")],
            minimum_direct_comments=int(config["comment_threshold"]),
            disable_early_stop=True,
        )
        if detail.get("error") or detail.get("danger"):
            raise RuntimeError(
                f"Activity assessment incomplete: {detail.get('danger') or detail.get('reason') or detail.get('error')}"
            )
        candidate["activity_counts"] = activity_counts(
            detail, int(config["activity_window_days"]) * 24
        )
        candidate["very_active"] = qualifies(candidate["activity_counts"], config)
        candidate["recommendation"] = recommendation_for(candidate, config)
        candidate["activity_assessment_status"] = "complete"
        candidate["activity_assessed_at"] = now().isoformat()
        candidate.pop("activity_error", None)
        return True
    except Exception as error:
        candidate["activity_assessment_status"] = "failed"
        candidate["activity_error"] = str(error)[:500]
        return False
    finally:
        cdp = getattr(session, "cdp", None)
        if cdp is not None:
            cdp.execution_observer = None


def upsert_high_signal(candidate: dict[str, Any]) -> None:
    state = read_json(HIGH_SIGNAL_PATH, {"profiles": []})
    rows = state.setdefault("profiles", [])
    existing = next(
        (row for row in rows if row.get("profile_url") == candidate.get("profile_url")), None
    )
    payload = {
        key: candidate.get(key)
        for key in (
            "profile_url",
            "name",
            "location",
            "follower_count",
            "activity_counts",
            "source_post",
            "country_code",
            "region",
        )
    }
    payload["last_seen_at"] = now().isoformat()
    if existing:
        existing.update(payload)
    else:
        payload["first_seen_at"] = now().isoformat()
        rows.append(payload)
    write_json(HIGH_SIGNAL_PATH, state)
