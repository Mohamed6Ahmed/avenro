"""Creates/updates .env from questions. Run through setup.bat."""
import os, re, secrets, shutil
root = os.path.dirname(os.path.abspath(__file__)); env = os.path.join(root, ".env")
if not os.path.exists(env): shutil.copy(os.path.join(root, ".env.example"), env)
lines = open(env, encoding="utf-8-sig").read().splitlines()
def get(k):
    for l in lines:
        m = re.match(r"\s*%s\s*=(.*)" % k, l)
        if m: return m.group(1).strip()
    return ""
def put(k, v):
    for i, l in enumerate(lines):
        if re.match(r"\s*%s\s*=" % k, l): lines[i] = "%s=%s" % (k, v); return
    lines.append("%s=%s" % (k, v))
def ask(k, q, secret=False):
    cur = get(k); v = input("%s%s: " % (q, " [Enter = keep current]" if cur else " [Enter = skip]")).strip().strip('"').strip("'")
    if v: put(k, v)
print("\nAvenro setup. Press Enter to skip anything you do not have yet. You can run this again any time.\n")
ask("GOOGLE_CLIENT_ID", "Google Client ID")
ask("GOOGLE_CLIENT_SECRET", "Google Client secret")
ask("VODAFONE_CASH_NUMBER", "Your Vodafone Cash number (e.g. 010xxxxxxxx)")
ask("VODAFONE_CASH_NAME", "Name on the Vodafone Cash wallet")
ask("INSTAPAY_ADDRESS", "Your InstaPay address (e.g. name@instapay)")
ask("INSTAPAY_NAME", "Name on InstaPay")
if not get("ADMIN_TOKEN"):
    t = secrets.token_urlsafe(12); put("ADMIN_TOKEN", t); print("\nAdmin password created for /admin.html: " + t + "   (also saved in .env)")
open(env, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("\nSaved to .env. Now close the Avenro window and run start.bat again.")
