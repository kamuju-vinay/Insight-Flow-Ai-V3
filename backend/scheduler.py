import re
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Plan schedule times are set by users based on their local (IST) clock —
# pin the scheduler to that timezone so a "06:30" entry fires at 06:30 IST
# no matter what timezone the Render host's OS is running.
scheduler = BackgroundScheduler(timezone="Asia/Kolkata")


def email_job_wrapper(plan_id):
    from backend.db import get_plan, get_articles, save_log
    from backend.email_service import send_digest_for_plan

    plan = get_plan(plan_id)
    if not plan:
        return

    articles = get_articles(plan_id)
    if not articles:
        save_log(f"⚠️ Scheduled email skipped for plan \"{plan['name']}\": no articles yet.", plan["name"], "warn")
        return

    save_log(f"📧 Scheduler triggered email send for plan: {plan['name']}", plan["name"], "email")
    try:
        result = send_digest_for_plan(plan, articles)
        save_log(
            f"✅ Scheduled email complete: {result['sent']} sent, {result['failed']} failed.",
            plan["name"], "email"
        )
    except Exception as e:
        save_log(f"❌ Scheduled email failed: {e}", plan["name"], "error")

def job_wrapper(plan_id):
    from backend.db import get_settings, save_log, get_plan
    from backend.crawler import run_crawl_backend
    
    plan = get_plan(plan_id)
    if not plan:
        return
        
    save_log(f"⏰ Scheduler triggered background crawl for plan: {plan['name']}", plan["name"], "info")
    config = get_settings()
    
    try:
        run_crawl_backend(plan_id)
    except Exception as e:
        save_log(f"❌ Scheduled crawl failed: {e}", plan["name"], "error")

def register_plan_job(plan):
    remove_plan_job(plan["id"])
    
    if plan.get("status") != "running":
        return
        
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
            elif period == "week":
                t_str = trigger_times.get("week", "06:30")
                h, m = map(int, t_str.split(":"))
                scheduler.add_job(
                    job_wrapper,
                    trigger=CronTrigger(day_of_week="mon", hour=h, minute=m),
                    id=job_id,
                    args=[plan["id"]],
                    replace_existing=True
                )
                print(f"[Scheduler] Registered weekly job {job_id} on Mon at {t_str}")
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
        except Exception as e:
            print(f"❌ [Scheduler] Error registering job {job_id}: {e}")

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
