# Amazon Packing Automation – Website

Upload Amazon invoices + SKU Master Sheet → get stamped PDFs + summary.

Works on phone and laptop.

---

## Deploy on Render.com (Free)

### Step 1: Create account
1. Go to [https://render.com](https://render.com)
2. Sign up with Google or email (free)

### Step 2: Create the Web Service
1. Click **New +** → **Web Service**
2. Choose **Build and deploy from a Git repository** (or “Public Git repository” if you upload to GitHub)
   - **Easiest if you don’t use GitHub:**  
     Use the “Deploy a Docker / upload files” option if available,  
     or create a free GitHub account, upload the files there, then connect it.

   **Simple method (recommended):**
   - Create a free GitHub account
   - Create a new repository
   - Upload all the files from this folder (`app.py`, `requirements.txt`, `Procfile`, `templates` folder)
   - Then on Render: New → Web Service → connect that GitHub repo

3. Settings:
   - **Name**: packing-automation (or any name)
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT`
   - **Instance Type**: Free

4. (Optional) Add Environment Variable:
   - Key: `TEAM_PASSWORD`
   - Value: any password you want for your team

5. Click **Create Web Service**

### Step 3: Wait
Render will build and deploy (takes 2–5 minutes).  
When it says “Live”, you get a link like:

`https://packing-automation.onrender.com`

Share that link with your team. They can open it on phone or laptop.

---

## Files needed
- `app.py`
- `requirements.txt`
- `Procfile`
- `templates/` folder (with `index.html` and `login.html`)

That’s it.
