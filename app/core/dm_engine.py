import time
import random
import os
import sqlite3
import json
from pathlib import Path
from datetime import datetime

from PyQt6.QtCore import QThread, pyqtSignal
from playwright.sync_api import sync_playwright, TimeoutError
try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

from core.account_manager import AccountManager
from core.queue_manager import QueueManager
from core.message_builder import get_random_message
from core.tracker import Tracker

class SkipUserException(Exception):
    pass

class DMEngine(QThread):
    log_signal = pyqtSignal(str, str)            # type, msg
    progress_signal = pyqtSignal(int, int)         # sent_count, failed_count
    status_signal = pyqtSignal(str)                # Engine status message
    account_switched_signal = pyqtSignal(str)      # Username
    finished_signal = pyqtSignal()
    restriction_signal = pyqtSignal(str, str)      # account_username, reason
    queue_completed_signal = pyqtSignal(int, int)  # sent_count, failed_count
    
    def __init__(self):
        super().__init__()
        self.is_running = False
        self.is_paused = False
        self.skip_requested = False
        
        self.account_manager = AccountManager()
        self.queue_manager = QueueManager()
        self.tracker = Tracker()
        self.consecutive_failures = 0  # tracks back-to-back real failures
        self.followup_only = False     # set via dashboard toggle before start
        self.engage_only = False       # follow+like but skip the DM
        
        self._load_settings()
        
    def _load_settings(self):
        # Use absolute path anchored to this file's location so the correct
        # config is loaded regardless of the process working directory.
        settings_path = Path(__file__).parent.parent / 'config' / 'settings.json'
        if settings_path.exists():
            with open(settings_path, 'r') as f:
                self.settings = json.load(f)
        else:
            self.settings = {"delay_min": 45, "delay_max": 120}

        # Load attachment path from messages.json (follow-up only)
        messages_path = Path(__file__).parent.parent / 'config' / 'messages.json'
        self.followup_attachment = ''
        self.followup_2_attachment = ''
        if messages_path.exists():
            with open(messages_path, 'r', encoding='utf-8') as f:
                msg_data = json.load(f)
                self.followup_attachment = msg_data.get('followup_attachment', '')
                self.followup_2_attachment = msg_data.get('followup_2_attachment', '')
            
    def stop(self):
        self.is_running = False
        self.log_signal.emit("WARN", "Stopping engine...")

    def set_followup_only(self, value: bool):
        """Called by main_window before starting the engine."""
        self.followup_only = value

    def set_engage_only(self, value: bool):
        """
        Engage-only mode: follow + like each target but send NO DM.

        Useful when Instagram has rate-limited DM volume but follows/likes
        still work, or to warm a fresh batch before messaging them.
        Targets are marked 'Engaged' so they are not re-processed.
        """
        self.engage_only = value
        
    def pause(self):
        self.is_paused = True
        self.log_signal.emit("INFO", "Engine paused")
        
    def resume(self):
        self.is_paused = False
        self.log_signal.emit("INFO", "Engine resumed")
        
    def _check_skip(self):
        if self.skip_requested:
            raise SkipUserException("User skipped by request")

    def _random_sleep(self, min_s=None, max_s=None):
        if min_s is None: min_s = self.settings.get("delay_min", 45)
        if max_s is None: max_s = self.settings.get("delay_max", 120)
        
        if self.settings.get("use_gaussian_delays", False):
            # Gaussian bell-curve: mean = midpoint, std = 1/6 of the range
            mean = (min_s + max_s) / 2
            std = (max_s - min_s) / 6
            delay = max(min_s, min(max_s, random.gauss(mean, std)))
        else:
            delay = random.uniform(min_s, max_s)
            
        self.log_signal.emit("INFO", f"Waiting {delay:.1f}s before next action...")
        
        # Non-blocking sleep for QThread
        slept = 0.0
        while slept < delay and self.is_running:
            if self.skip_requested:
                break
            while self.is_paused and self.is_running:
                time.sleep(1)
            time.sleep(0.5)
            slept += 0.5

    def _human_delay_with_browsing(self, page, min_s=None, max_s=None):
        """
        Inter-DM cooldown that browses the Instagram home feed instead of
        sitting idle. This makes the account pattern look significantly more
        human — real users read their feed between conversations.
        """
        if min_s is None: min_s = self.settings.get("delay_min", 45)
        if max_s is None: max_s = self.settings.get("delay_max", 120)

        if self.settings.get("use_gaussian_delays", False):
            mean = (min_s + max_s) / 2
            std  = (max_s - min_s) / 6
            delay = max(min_s, min(max_s, random.gauss(mean, std)))
        else:
            delay = random.uniform(min_s, max_s)

        self.log_signal.emit("INFO", f"Cooling down {delay:.1f}s — scrolling home feed to appear human...")

        elapsed = 0.0
        try:
            # Navigate to home feed
            page.goto("https://www.instagram.com/", timeout=30000, wait_until="domcontentloaded")
            time.sleep(1.5)
            elapsed += 1.5

            while elapsed < delay and self.is_running:
                if self.skip_requested:
                    break
                while self.is_paused and self.is_running:
                    time.sleep(1)
                    elapsed += 1

                # Scroll by a random natural amount
                scroll_px = random.randint(250, 700)
                page.evaluate(f"window.scrollBy(0, {scroll_px})")
                self.log_signal.emit("INFO", f"[Feed] Scrolled {scroll_px}px — {delay - elapsed:.0f}s remaining")

                # Pause between scrolls for a human-like read time
                pause = min(random.uniform(3.0, 7.0), delay - elapsed)
                if pause > 0:
                    time.sleep(pause)
                    elapsed += pause

        except Exception:
            # If navigation fails (e.g. network hiccup) fall back to plain sleep
            self.log_signal.emit("WARN", "Feed browse failed — falling back to plain wait")
            remaining = max(0.0, delay - elapsed)
            slept = 0.0
            while slept < remaining and self.is_running:
                if self.skip_requested:
                    break
                time.sleep(0.5)
                slept += 0.5

    def _check_anti_ban(self, page):
        """Check for any Instagram restriction, block, or rate-limit signal."""
        try:
            url = page.url
            if "challenge" in url or "suspended" in url or "disabled" in url:
                return "Account Suspended / Challenge Required"

            # Hard action-block patterns
            hard_patterns = [
                "Try Again Later",
                "Action Blocked",
                "We restrict certain activity",
                "Your account has been temporarily blocked",
                "We\'ve detected unusual activity",
            ]
            for pattern in hard_patterns:
                if page.locator(f"text='{pattern}'").count() > 0:
                    return f"Instagram Hard Block: {pattern}"

            # Soft rate-limit warnings
            soft_patterns = [
                "We limit how often",
                "too many requests",
            ]
            for pattern in soft_patterns:
                if page.locator(f"text='{pattern}'").count() > 0:
                    return f"Rate-Limit Warning: {pattern}"

        except Exception:
            pass

        return None

    def _engage_comment(self, page, username, text):
        """
        Like is done; now post a comment on the currently-open post modal.

        Returns True if a comment was submitted. Silent-fail throughout.
        The post modal must already be open (call this right after liking).
        """
        if not text:
            return False
        try:
            # The comment box inside the post modal.
            box = None
            for sel in ("textarea[aria-label='Add a comment…']",
                        "textarea[aria-label*='comment']",
                        "textarea[placeholder*='comment']",
                        "form textarea",
                        "div[role='textbox'][contenteditable='true']"):
                try:
                    loc = page.locator(sel).last
                    if loc.count() > 0:
                        box = loc
                        break
                except Exception:
                    continue
            if box is None:
                return False

            try:
                box.click(timeout=4000)
            except Exception:
                box.click(force=True, timeout=4000)
            self._random_sleep(0.8, 1.8)

            page.keyboard.insert_text(text)
            self._random_sleep(1.0, 2.5)
            self._check_skip()

            # Submit: Enter, or a 'Post' button
            posted = False
            for sel in ("div[role='button']:has-text('Post')", "button:has-text('Post')"):
                try:
                    btn = page.locator(sel).last
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=4000)
                        posted = True
                        break
                except Exception:
                    continue
            if not posted:
                page.keyboard.press("Enter")

            self._random_sleep(1.5, 3.0)
            return True
        except Exception:
            return False

    # ── Profile engagement: follow, then like N posts ─────────────────────────

    def _engage_follow(self, page, username):
        """
        Click Follow on the target profile. Returns 'followed' | 'already' | 'failed'.

        Deliberately silent-fail: a failed follow must never abort the DM.
        """
        try:
            # The Follow button is a role=button containing exactly 'Follow'.
            # 'Following' and 'Follow Back' both start with 'Follow', so match
            # the exact text to avoid clicking an already-followed button.
            candidates = page.locator("div[role='button'], button")
            count = min(candidates.count(), 40)
            for i in range(count):
                try:
                    el = candidates.nth(i)
                    txt = (el.inner_text() or "").strip()
                except Exception:
                    continue

                if txt == "Follow":
                    try:
                        el.click(timeout=4000)
                    except Exception:
                        el.click(force=True, timeout=4000)
                    self.log_signal.emit("INFO", f"Followed @{username}")
                    return "followed"
                if txt in ("Following", "Follow Back"):
                    return "already"
            return "failed"
        except SkipUserException:
            raise
        except Exception:
            return "failed"

    def _engage_like_posts(self, page, username, n_likes, comment_text=None):
        """
        Open the target's posts and like up to n_likes DISTINCT posts.

        If comment_text is given, the FIRST post opened also gets that comment
        (only one comment per person, so it stays natural).

        Returns the number of posts actually liked. Silent-fail throughout:
        a profile with no posts, or a slow grid, must not abort the DM.
        """
        liked = 0
        commented_flag = [False]
        try:
            page.wait_for_selector("a[href*='/p/'], a[href*='/reel/']", timeout=5000)
        except Exception:
            return 0

        try:
            posts = page.locator("a[href*='/p/'], a[href*='/reel/']")
            total = posts.count()
            if total == 0:
                return 0

            # Pick distinct indexes so we never like the same post twice.
            # Look at the 9 most recent posts so a request for up to 6 likes
            # still has headroom (some may already be liked).
            pool_size = min(total, max(9, n_likes + 3))
            pool = list(range(pool_size))
            random.shuffle(pool)
            targets = pool[:min(n_likes, len(pool))]

            for idx in targets:
                if not self.is_running:
                    break
                self._check_skip()
                try:
                    page.locator("a[href*='/p/'], a[href*='/reel/']").nth(idx).click()
                    self._random_sleep(2, 4)
                    self._check_skip()

                    # Only like if not already liked (aria-label flips to Unlike)
                    like_btn = page.locator("svg[aria-label='Like']").first
                    if like_btn.count() > 0:
                        try:
                            like_btn.click(force=True, timeout=4000)
                        except Exception:
                            pass
                        liked += 1
                        self.log_signal.emit("INFO", f"Liked post {liked}/{len(targets)} on @{username}")
                    else:
                        # Already liked - count it so pacing still looks natural
                        self.log_signal.emit("INFO", f"Post already liked on @{username}")

                    self._random_sleep(1, 3)
                    self._check_skip()

                    # Comment on the FIRST post only (one comment per person)
                    if comment_text and not commented_flag[0]:
                        if self._engage_comment(page, username, comment_text):
                            commented_flag[0] = True
                            self.log_signal.emit("INFO", f"Commented on @{username}: {comment_text}")
                            self._random_sleep(1.5, 3)
                            self._check_skip()

                    page.keyboard.press("Escape")
                    self._random_sleep(1, 2)
                    self._check_skip()
                except SkipUserException:
                    raise
                except Exception:
                    # Close any half-open modal and carry on with the next post
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    self._random_sleep(1, 2)
                    continue
        except SkipUserException:
            raise
        except Exception:
            pass

        return liked

    def _setup_browser(self, p, account):
        account_id = account['id'] if isinstance(account, dict) else account
        proxy_str = account.get('proxy', '') if isinstance(account, dict) else ''
        
        # Ensure profile folder exists
        profile_dir = Path(f'data/profiles/account_{account_id}')
        profile_dir.mkdir(parents=True, exist_ok=True)
        
        launch_kwargs = dict(
            user_data_dir=str(profile_dir.absolute()),
            channel="msedge",
            headless=False,
            no_viewport=True,
            ignore_default_args=['--enable-automation'],
            args=[
                '--disable-blink-features=AutomationControlled',
                '--test-type',
                '--window-size=1280,800'
            ]
        )
        
        # Inject proxy if configured for this account
        if proxy_str and proxy_str.strip():
            p_str = proxy_str.strip()
            # Normalize: if no protocol prefix, add http://
            if not p_str.startswith('http'):
                p_str = 'http://' + p_str
            launch_kwargs['proxy'] = {'server': p_str}
            self.log_signal.emit("INFO", f"Using proxy: {p_str} for account {account_id}")
        
        context = p.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        
        # Apply stealth scripts to hide automation fingerprints
        if STEALTH_AVAILABLE:
            stealth_sync(page)
            self.log_signal.emit("INFO", "Browser stealth mode active")
        
        return context, page

    def run(self):
        self.is_running = True
        self._load_settings()
        
        sent_count = 0
        failed_count = 0
        
        try:
            with sync_playwright() as p:
                current_account = self.account_manager.get_next_available()
                if not current_account:
                    self.log_signal.emit("ERROR", "No active accounts available with remaining daily limit.")
                    self.status_signal.emit("Failed: No accounts")
                    return
                    
                self.log_signal.emit("INFO", f"Starting engine with account: {current_account['username']}")
                self.account_switched_signal.emit(current_account['username'])
                
                context, page = self._setup_browser(p, current_account)
                
                # Verify login
                page.goto("https://www.instagram.com/", timeout=60000)
                if "login" in page.url or page.locator("input[name='username']").count() > 0:
                    self.log_signal.emit("ERROR", f"Account {current_account['username']} is not logged in! Please login manually first.")
                    self.account_manager.update_status(current_account['id'], "Blocked")
                    context.close()
                    return
                
                queue = self.queue_manager.get_pending()
                pre_filter = len(queue)

                # Follow-up Only Mode: STRICT — only people already queued as a
                # follow-up. Plain 'Pending' new leads are skipped entirely, so
                # this toggle must be OFF when working a fresh list.
                if self.followup_only:
                    queue = [t for t in queue if t.get('status') == 'Pending Followup']
                    self.log_signal.emit("INFO",
                        f"Follow-up Only Mode active — {len(queue)} follow-up task(s) queued.")
                    if not queue and pre_filter:
                        self.log_signal.emit(
                            "WARN",
                            f"Follow-up Only is ON but none of the {pre_filter} queued targets "
                            f"are follow-ups. Turn it OFF to reach a fresh list."
                        )

                # Engage-only: only touch rows that have not been engaged yet.
                if self.engage_only:
                    before = len(queue)
                    queue = [t for t in queue if t.get('status') != 'Engaged']
                    if before != len(queue):
                        self.log_signal.emit(
                            "INFO",
                            f"Engage-Only Mode — {len(queue)} target(s) not yet engaged."
                        )

                # Nothing to do? Say so clearly instead of silently finishing.
                if not queue:
                    if pre_filter == 0:
                        self.log_signal.emit("WARN",
                            "Queue is empty — nothing to process. Import targets on the Queue tab.")
                        self.status_signal.emit("Finished: queue empty")
                    else:
                        self.log_signal.emit("WARN",
                            f"All {pre_filter} queued targets were filtered out by the "
                            f"active toggles (Follow-up Only / Engage Only). Nothing to do.")
                        self.status_signal.emit("Finished: nothing matched filters")
                    self.queue_completed_signal.emit(0, 0)
                    try:
                        context.close()
                    except Exception:
                        pass
                    return

                if self.settings.get('send_order') == 'random':
                    random.shuffle(queue)
                    
                self.log_signal.emit("INFO", f"Found {len(queue)} pending targets in queue.")
                
                for task in queue:
                    if not self.is_running:
                        break
                    while self.is_paused:
                        time.sleep(1)
                        if not self.is_running: break
                        
                    target_username = task['username']
                    task_id = task['id']
                    
                    # 1. Determine follow-up level based on DB sent history (outreach auto-promotion)
                    dm_count = self.queue_manager.get_sent_count(target_username)
                    is_followup = (task.get('status') == 'Pending Followup')
                    
                    if is_followup or dm_count > 0:
                        is_followup = True
                        if dm_count == 0:
                            followup_level = 1
                        else:
                            # 1 sent DM means next is Follow-up 1 (sec level 1).
                            # 2 or more sent DMs means next is Follow-up 2 (sec level 2).
                            followup_level = 1 if dm_count == 1 else 2
                    else:
                        is_followup = False
                        followup_level = 0
                    
                    self.log_signal.emit("INFO", f"Processing: {target_username} (DMs sent previously: {dm_count}, Level: {followup_level})")
                    
                    try:
                        self.skip_requested = False
                        self._check_skip()
                        
                        # 1. Navigate to Target Profile
                        page.goto(f"https://www.instagram.com/{target_username}/", timeout=30000)
                        self._random_sleep(3, 6) # wait for page load fully organically
                        self._check_skip()
                        
                        # Anti-ban check
                        ban_reason = self._check_anti_ban(page)
                        if ban_reason:
                            raise Exception(ban_reason)
                            
                        # Human simulation: scroll target profile briefly before DM
                        page.evaluate("window.scrollBy(0, 500)")
                        self._random_sleep(1, 3)
                        self._check_skip()
                        
                        # 2. Engage: Follow, then like 1-4 posts (config-driven)
                        engage_on = self.settings.get('engage_enabled', True)
                        if engage_on:
                            if self.settings.get('engage_follow', True):
                                res = self._engage_follow(page, target_username)
                                if res == "followed":
                                    self.tracker.mark_followed(target_username)
                                self._random_sleep(1, 3)
                                self._check_skip()

                            lo = int(self.settings.get('engage_like_min', 1))
                            hi = int(self.settings.get('engage_like_max', 4))
                            if hi < lo:
                                lo, hi = hi, lo
                            n_likes = random.randint(lo, hi) if hi > 0 else 0

                            if n_likes > 0:
                                c_text = None
                                if self.settings.get('engage_comment', False):
                                    c_text = self.tracker.get_random_comment()
                                done = self._engage_like_posts(
                                    page, target_username, n_likes, comment_text=c_text
                                )
                                if done > 0:
                                    self.tracker.mark_liked(target_username, done)
                                if c_text and done > 0:
                                    self.tracker.mark_commented(target_username, c_text)
                                self.log_signal.emit(
                                    "INFO",
                                    f"Engagement on @{target_username}: liked {done}/{n_likes} post(s)"
                                )
                                self._random_sleep(1, 3)
                                self._check_skip()
                        
                        # ── Engage-only mode: stop here, no DM ──────────────
                        if self.engage_only:
                            for p in page.context.pages:
                                pass
                            self.queue_manager.update_status(task_id, "Engaged")
                            sent_count += 1
                            self.progress_signal.emit(sent_count, failed_count)
                            self.log_signal.emit("INFO", f"Engaged @{target_username} (no DM)")
                            self._human_delay_with_browsing(page)
                            continue

                        # 3. Click 'Message' button
                        # Instagram desktop uses divs with role="button" for these
                        msg_btn = page.locator("div[role='button']:has-text('Message'), button:has-text('Message')").first
                        if msg_btn.count() == 0:
                             # Fallback to direct text search
                             msg_btn = page.locator("text='Message'").first
                             
                        if msg_btn.count() == 0:
                             raise Exception("Message button not found on profile (Private account, you must follow them, or Instagram hid the button)")
                             
                        self._check_skip()
                        msg_btn.click()
                        self._random_sleep(4, 8)
                        self._check_skip()
                        
                        # Dismiss potential "Turn on Notifications" modal if it pops up
                        not_now = page.locator("button:has-text('Not Now')").first
                        if not_now.count() > 0:
                            not_now.click()
                            self._random_sleep(1, 2)
                        
                        self._check_skip()
                        
                        # 5. Type and send message
                        # Wait for the chat DM interface to load.
                        # 15s was too tight: on a slow connection Instagram's DM
                        # composer can take longer, which caused hard failures.
                        # Retry once at a longer timeout before giving up.
                        dm_timeout = int(self.settings.get('dm_timeout_ms', 40000))
                        try:
                            page.wait_for_selector("div[role='textbox']", timeout=dm_timeout)
                        except Exception:
                            self.log_signal.emit(
                                "WARN",
                                f"DM composer slow on @{target_username} — retrying with longer wait"
                            )
                            self._random_sleep(2, 4)
                            page.wait_for_selector("div[role='textbox']", timeout=dm_timeout * 2)
                        
                        # There might be multiple textboxes (like search). The actual message box is typically the last visible one.
                        messagebox = page.locator("div[role='textbox']").last
                        
                        msg = get_random_message(target_username, followup_level)
                        if not msg:
                             raise Exception("Message pool is empty")
                             
                        messagebox.click()
                        
                        # Paste instantly instead of slow typing, as requested
                        page.keyboard.insert_text(msg)
                        self._random_sleep(1, 3)
                        self._check_skip()
                        
                        # Actually send text message
                        page.keyboard.press("Enter")

                        # ── Follow-up Attachment (silent fail) ─────────────────
                        active_attachment = ''
                        if is_followup:
                            if followup_level == 1:
                                active_attachment = self.followup_attachment
                            elif followup_level >= 2:
                                active_attachment = self.followup_2_attachment

                        if is_followup and active_attachment:
                            attach_path = Path(active_attachment)
                            if attach_path.exists():
                                try:
                                    self._random_sleep(1, 2)
                                    self._check_skip()

                                    # Instagram DM toolbar attachment selectors (priority order)
                                    _attach_selectors = [
                                        # 2024-2025 Instagram DM — image/media icon
                                        "[aria-label='Add Photo or Video']",
                                        "[aria-label='Photo or Video']",
                                        "[aria-label='Attach']",
                                        "[aria-label='Clip']",
                                        # Generic aria-label partial matches
                                        "[aria-label*='Photo']",
                                        "[aria-label*='Video']",
                                        "[aria-label*='Media']",
                                        "[aria-label*='File']",
                                        "[aria-label*='Attach']",
                                        # SVG icon buttons in compose toolbar
                                        "svg[aria-label*='Photo']",
                                        "svg[aria-label*='Media']",
                                    ]

                                    clip_btn = None
                                    for sel in _attach_selectors:
                                        try:
                                            candidate = page.locator(sel).first
                                            if candidate.count() > 0:
                                                candidate.wait_for(state="visible", timeout=2000)
                                                clip_btn = candidate
                                                self.log_signal.emit("INFO",
                                                    f"Attachment button found via: {sel}")
                                                break
                                        except Exception:
                                            continue

                                    # Last-resort: try injecting directly into a hidden file input
                                    if clip_btn is None:
                                        file_inputs = page.locator("input[type='file']")
                                        if file_inputs.count() > 0:
                                            file_inputs.first.set_input_files(str(attach_path))
                                            self._random_sleep(2, 4)
                                            self._check_skip()
                                            page.keyboard.press("Enter")
                                            self.log_signal.emit("INFO",
                                                f"Attachment sent via hidden input: {attach_path.name}")
                                        else:
                                            raise Exception(
                                                "No attachment button or file input found on page — "
                                                "Instagram may have updated its DM UI"
                                            )
                                    else:
                                        with page.expect_file_chooser(timeout=10000) as fc_info:
                                            clip_btn.click(force=True)
                                        file_chooser = fc_info.value
                                        file_chooser.set_files(str(attach_path))
                                    self._random_sleep(2, 4)
                                    self._check_skip()
                                    page.keyboard.press("Enter")
                                    self.log_signal.emit("INFO", f"Attachment sent: {attach_path.name}")
                                except SkipUserException as skip_e:
                                    raise skip_e
                                except Exception as attach_err:
                                    self.log_signal.emit("WARN",
                                        f"Attachment skipped for {target_username}: {attach_err}")
                            else:
                                self.log_signal.emit("WARN",
                                    f"Attachment file not found: {active_attachment}")

                        # Post-send restriction check — give Instagram 2s to react
                        time.sleep(2.0)
                        post_send_ban = self._check_anti_ban(page)
                        if post_send_ban:
                            raise Exception(post_send_ban)

                        # 6. Success logic
                        self.consecutive_failures = 0  # reset streak on success
                        self.log_signal.emit("SUCCESS", f"Sent DM to {target_username}")
                        self.queue_manager.update_status(task_id, "Sent")
                        self.queue_manager.log_sent(target_username, current_account['id'], msg)
                        self.tracker.mark_dmed(target_username)
                        self.account_manager.increment_dm_count(current_account['id'])
                        
                        if is_followup:
                            self.queue_manager.mark_followup_sent(target_username)
                            
                        sent_count += 1
                        self.progress_signal.emit(sent_count, failed_count)
                        
                        # 7. Check account rotation
                        current_account = self.account_manager.get_account_by_id(current_account['id'])
                        if current_account['dms_sent_today'] >= current_account['daily_limit']:
                            self.log_signal.emit("WARN", f"Daily limit reached for {current_account['username']}")
                            context.close()
                            
                            next_acc = self.account_manager.get_next_available()
                            if not next_acc:
                                self.log_signal.emit("WARN", "No more active accounts available.")
                                break
                                
                            current_account = next_acc
                            self.log_signal.emit("INFO", f"Switching to account {current_account['username']}")
                            self.account_switched_signal.emit(current_account['username'])
                            context, page = self._setup_browser(p, current_account)
                            
                    except SkipUserException as skip_e:
                        self.log_signal.emit("INFO", f"Skipped target {target_username}: {skip_e}")
                        self.queue_manager.update_status(task_id, "Skipped")
                        continue
                    except Exception as e:
                        err_str = str(e)

                        # Private/no-message-button: skip, do NOT count as failure
                        if "Private account" in err_str or "No Msg Button" in err_str:
                             self.log_signal.emit("WARN", f"Skipped {target_username}: Account private or messages off")
                             self.queue_manager.update_status(task_id, "Skipped", "Private/No Msg Button")
                             self._random_sleep(1, 2)
                             continue

                        # Real failure — count it
                        self.consecutive_failures += 1
                        self.log_signal.emit("ERROR", f"Failed targeting {target_username}: {err_str}")
                        self.queue_manager.update_status(task_id, "Failed", err_str)
                        failed_count += 1
                        self.progress_signal.emit(sent_count, failed_count)

                        # ⚠ Early warning at 3 consecutive failures
                        if self.consecutive_failures == 3:
                            warn_msg = (f"⚠ 3 consecutive failures on @{current_account['username']}. "
                                        f"Instagram may be throttling this account.")
                            self.log_signal.emit("WARN", warn_msg)
                            self.restriction_signal.emit(
                                current_account['username'],
                                "3 consecutive DM failures — account may be getting throttled. Watch closely."
                            )

                        # 🛑 Hard stop at 5 consecutive failures
                        if self.consecutive_failures >= 5:
                            self.log_signal.emit("ERROR",
                                f"🛑 5 consecutive failures. Stopping to protect @{current_account['username']}.")
                            self.restriction_signal.emit(
                                current_account['username'],
                                "5 consecutive DM failures — engine stopped to protect this account."
                            )
                            self.account_manager.update_status(current_account['id'], "Restricted")
                            self.is_running = False
                            break

                        # Instagram hard block / restriction — rotate account immediately
                        is_hard_block = any(x in err_str for x in [
                            "Hard Block", "Suspended", "Rate-Limit",
                            "Challenge Required", "Action Blocked"
                        ])
                        if is_hard_block:
                            self.log_signal.emit("ERROR",
                                f"🛑 Instagram restriction on @{current_account['username']} — rotating account.")
                            self.restriction_signal.emit(current_account['username'], err_str)
                            self.account_manager.update_status(current_account['id'], "Blocked")
                            context.close()

                            next_acc = self.account_manager.get_next_available()
                            if not next_acc:
                                self.log_signal.emit("WARN", "All accounts blocked or exhausted.")
                                break
                            current_account = next_acc
                            self.consecutive_failures = 0  # reset for new account
                            self.log_signal.emit("INFO", f"Switched to @{current_account['username']}")
                            self.account_switched_signal.emit(current_account['username'])
                            context, page = self._setup_browser(p, current_account)
                            
                    # Delay before next user — browse home feed to look human
                    self._human_delay_with_browsing(page)

                # ── Queue exhausted naturally (not stopped by user) ──
                if self.is_running:
                    self.log_signal.emit("INFO", "✅ All queued DMs have been processed.")
                    self.queue_completed_signal.emit(sent_count, failed_count)

        except Exception as e:
            self.log_signal.emit("ERROR", f"Engine crash: {str(e)}")
            
        finally:
            self.is_running = False
            self.status_signal.emit("Engine stopped")
            self.finished_signal.emit()
            self.log_signal.emit("INFO", f"Session ended. Sent: {sent_count}, Failed: {failed_count}")
