import re
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Plan schedule times are set by users based on their local (IST) clock —
# pin the scheduler to that timezone so a "06:30" entry fires at 06:30 IST
# no matter what timezone the Render host's OS is running.
#
# MEMORY: APScheduler's default BackgroundScheduler uses a 10-worker thread
# pool with no per-job overlap limit. That meant every plan's crawl could
# fire concurrently — each one opening its own async crawl with ~15 HTTP
# connections and holding page content in memory — which is what was
# blowing past Railway's 1GB memory cap and causing the OOM kills seen in
# the deploy activity log. Capping max_workers to 2 and max_instances to 1
# means at most 2 plan crawls run at the same time, and a single plan can
# never stack a second run on top of one still in progress.
scheduler = BackgroundScheduler(
    timezone="Asia/Kolkata",
    executors={"default": ThreadPoolExecutor(max_workers=2)},
    job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 300},
)


def email_job_wrapper(plan_id):
    from backend.db import get_plan, get_articles, save_log
    from backend.email_service import send_digest_for_plan

    plan = get_plan(plan_id)
    if not plan:
        return

    articles = get_articles(plan_id)
    if not articles:
        save_log(f"⚠️ Scheduled email skipped for plan \"{plan['name']}\": no articles yet.", plan["name"], "warn", user_id=plan.get("user_id"))
        return

    save_log(f"📧 Scheduler triggered email send for plan: {plan['name']}", plan["name"], "email", user_id=plan.get("user_id"))
    try:
        result = send_digest_for_plan(plan, articles)
        save_log(
            f"✅ Scheduled email complete: {result['sent']} sent, {result['failed']} failed.",
            plan["name"], "email", user_id=plan.get("user_id")
        )
    except Exception as e:
        save_log(f"❌ Scheduled email failed: {e}", plan["name"], "error", user_id=plan.get("user_id"))

def job_wrapper(plan_id):
    from backend.db import get_settings, save_log, get_plan
    from backend.crawler import run_crawl_backend
    
    plan = get_plan(plan_id)
    if not plan:
        return
        
    save_log(f"⏰ Scheduler triggered background crawl for plan: {plan['name']}", plan["name"], "info", user_id=plan.get("user_id"))
    config = get_settings()
    
    try:
        run_crawl_backend(plan_id)
    except Exception as e:
        save_log(f"❌ Scheduled crawl failed: {e}", plan["name"], "error", user_id=plan.get("user_id"))

def register_plan_job(plan):
    remove_plan_job(plan["id"])
    
    if plan.get("status") != "running":
        return

    from backend.db import save_log
    registered_any = False
    periods = plan.get("periods", [])
    trigger_times = plan.get("triggerTimes", {})
    
    for period in periods:
        job_id = f"{plan['id']}_{period}"
        try:
            if period == "day":
                t_str = trigger_times.get("day", "06:30")
                h, m = map(int, t_str.split(":"))
                scheduler.add_job(
                    job_wrapper,
                    trigger=CronTrigger(hour=h, minute=m),
                    id=job_id,
                    args=[plan["id"]],
                    replace_existing=True
                )
                print(f"[Scheduler] Registered daily job {job_id} at {t_str}")
                registered_any = True
            elif period == "week":
                t_str = trigger_times.get("week", "06:30")
                h, m = map(int, t_str.split(":"))
                # Honor the actual selected days (schedWeekDays), matching the
                # client scheduler's default of weekdays Mon-Fri when unset.
                # Previously this was hardcoded to Monday only.
                day_map = {"sun": "sun", "mon": "mon", "tue": "tue", "wed": "wed",
                           "thu": "thu", "fri": "fri", "sat": "sat"}
                raw_days = plan.get("schedWeekDays") or ["Mon", "Tue", "Wed", "Thu", "Fri"]
                cron_days = ",".join(day_map[d[:3].lower()] for d in raw_days if d[:3].lower() in day_map) or "mon"
                scheduler.add_job(
                    job_wrapper,
                    trigger=CronTrigger(day_of_week=cron_days, hour=h, minute=m),
                    id=job_id,
                    args=[plan["id"]],
                    replace_existing=True
                )
                print(f"[Scheduler] Registered weekly job {job_id} on [{cron_days}] at {t_str}")
                registered_any = True
            elif period == "month":
                t_str = trigger_times.get("month", "06:30")
                h, m = map(int, t_str.split(":"))
                scheduler.add_job(
                    job_wrapper,
                    trigger=CronTrigger(day=1, hour=h, minute=m),
                    id=job_id,
                    args=[plan["id"]],
                    replace_existing=True
                )
                print(f"[Scheduler] Registered monthly job {job_id} on day 1 at {t_str}")
                registered_any = True
            elif "m" in period or "minute" in period:
                digits = re.findall(r"\d+", period)
                if digits:
                    mins = int(digits[0])
                    scheduler.add_job(
                        job_wrapper,
                        trigger=IntervalTrigger(minutes=mins),
                        id=job_id,
                        args=[plan["id"]],
                        replace_existing=True
                    )
                    print(f"[Scheduler] Registered interval job {job_id} every {mins} mins")
                    registered_any = True
            elif "h" in period or "hour" in period:
                digits = re.findall(r"\d+", period)
                if digits:
                    hrs = int(digits[0])
                    scheduler.add_job(
                        job_wrapper,
                        trigger=IntervalTrigger(hours=hrs),
                        id=job_id,
                        args=[plan["id"]],
                        replace_existing=True
                    )
                    print(f"[Scheduler] Registered interval job {job_id} every {hrs} hours")
                    registered_any = True
        except Exception as e:
            print(f"❌ [Scheduler] Error registering job {job_id}: {e}")
            try:
                from backend.db import save_log
                save_log(f"❌ Failed to schedule '{period}' crawl for plan: {e}", plan.get("name", ""), "error", user_id=plan.get("user_id"))
            except Exception:
                pass

    if registered_any:
        try:
            save_log(f"✅ Scheduled crawl job(s) registered — will run automatically on the server.", plan.get("name", ""), "info", user_id=plan.get("user_id"))
        except Exception:
            pass

    # ── Scheduled auto-mail (independent of the crawl schedule above) ──────
    if plan.get("autoMail") and plan.get("sendMode") == "scheduled" and plan.get("sendTime"):
        try:
            h, m = map(int, plan["sendTime"].split(":"))
            email_job_id = f"{plan['id']}_email_send"
            scheduler.add_job(
                email_job_wrapper,
                trigger=CronTrigger(hour=h, minute=m),
                id=email_job_id,
                args=[plan["id"]],
                replace_existing=True
            )
            print(f"[Scheduler] Registered daily email-send job {email_job_id} at {plan['sendTime']}")
        except Exception as e:
            print(f"❌ [Scheduler] Error registering email job for plan {plan['id']}: {e}")
            try:
                from backend.db import save_log
                save_log(f"❌ Failed to schedule auto-mail send: {e}", plan.get("name", ""), "error", user_id=plan.get("user_id"))
            except Exception:
                pass

def remove_plan_job(plan_id):
    # Retrieve all jobs and look for plan_id prefixes
    for job in list(scheduler.get_jobs()):
        if job.id.startswith(f"{plan_id}_"):
            try:
                scheduler.remove_job(job.id)
                print(f"[Scheduler] Removed job {job.id}")
            except Exception as e:
                pass

def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        print("⏰ [Scheduler] Background Scheduler started")
        load_all_jobs()

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        print("⏰ [Scheduler] Background Scheduler stopped")

def load_all_jobs():
    from backend.db import get_plans
    plans = get_plans()
    for plan in plans:
        register_plan_job(plan)
