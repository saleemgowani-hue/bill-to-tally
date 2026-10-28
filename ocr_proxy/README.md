# SN Softech Cloud AI Proxy — vendor-only, deploy once

Ye chhota server aapke (vendor) apne cloud account pe chalta hai — customer
ke laptop pe KABHI nahi jaata, na hi customer build ke zip me shamil hota
hai. Iska kaam: aapki asli paid Gemini API key ko surakshit apne paas rakhna,
aur har customer ke app se aane wali request ko unki licence key se verify
karke Gemini ko forward karna.

```
Customer's app  --(unki licence key + bill image)-->  Ye proxy  --(aapki Gemini key)-->  Gemini
                                                          |
                                                   har key ka daily limit check karta hai
```

## Deploy karne ke steps (Render.com — free/sasta, sabse aasaan)

1. [render.com](https://render.com) pe account banao (GitHub se sign in kar sakte ho)
2. Is `ocr_proxy/` folder ko ek alag chhota GitHub repo me push karo (ya
   poore BillToTallyAgent repo ko private rakh kar Render ko sirf is
   folder ko "Root Directory" bata do)
3. Render par **New → Web Service** → apna repo select karo
4. Settings:
   - **Root Directory:** `ocr_proxy` (agar poora repo push kiya hai)
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. **Environment** tab me ye variables daalo (`.env.example` dekho):
   - `LICENSE_SIGNING_SECRET` — bilkul wahi value jo aapke main app ke
     `.env` me `LICENSE_SIGNING_SECRET` hai (customer builds isi se signed
     hote hain)
   - `GEMINI_API_KEY` — aapki asli, paid Gemini API key
   - `GEMINI_MODEL` — (optional, default theek hai)
   - `DAILY_REQUEST_LIMIT` — (optional, default 60 bills/din per customer)
6. Deploy hone do (2-3 min lagte hain). Render aapko ek URL dega, jaise:
   `https://sn-cloud-ai.onrender.com`
7. Check karo: us URL ke peeche `/health` lagao
   (`https://sn-cloud-ai.onrender.com/health`) — browser me JSON dikhna
   chahiye jisme `"ok": true` ho.

Render ka free tier thoda slow start hota hai agar 15 min se koi request na
aayi ho (cold start, pehla request ~30s lag sakta hai) — agar ye dikkat de to
paid starter plan (~$7/month) le lena, woh hamesha jaga rehta hai.

## Ab main app ko is proxy se jodna

Har naye customer build me (`python make_customer_build.py "Customer"`), ye
URL khud-ba-khud customer ke `.env` me `OCR_PROXY_URL` ke roop me daal diya
jaata hai — bas ek baar apne VENDOR `.env` me (BillToTallyAgent ke root
folder wale `.env` me, jahan `LICENSE_SIGNING_SECRET` hai) ye line daal do:

```
OCR_PROXY_URL=https://sn-cloud-ai.onrender.com
```

Uske baad jo bhi naya customer build banaoge, usme ye URL automatically
shamil ho jayega — customer ko kuch bhi set karne ki zaroorat nahi, bas
Settings → AI/OCR me "SN Softech Cloud AI" engine select kare aur apni
licence key daale (wahi jo unhone activate karte waqt use ki thi).

## Security notes

- Is folder ka `.env` (real keys wala) kabhi bhi GitHub par push mat karna,
  aur kabhi customer build me shamil mat hona (ye `make_customer_build.py`
  ke `EXCLUDE_DIRS` me already add hai).
- Har licence key ka apna daily limit hai — agar ek key leak ho jaaye, nuksan
  bounded rahega (ek din me max `DAILY_REQUEST_LIMIT` bills tak).
- Proxy sirf "ye ek sahi SN Softech licence key hai ya nahi" check karta hai
  (signature verify) — ye activation/expiry check nahi karta (uske liye
  database chahiye hoga, jo is proxy ke paas nahi hai). Agar koi expired
  licence wala customer isko use kare to bhi chalega — agar aap ye bhi
  restrict karna chahte ho to batana, add kar denge.

## Cost ka andaza

- Render hosting: free ya ~$7/month (paid starter, zyada reliable)
- Gemini usage: jitna customers actually use utna hi — roughly ₹1-3 per bill
  (model aur bill size par depend karta hai)
