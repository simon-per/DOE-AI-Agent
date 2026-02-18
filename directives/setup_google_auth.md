# Directive: Google OAuth Setup

## Goal
One-time setup for Google Sheets, Drive, and Gmail API access via OAuth 2.0.

## Two Token Files
The pipeline uses **two separate OAuth tokens** for security separation:

| Token | Scopes | Used By |
|-------|--------|---------|
| `token.json` | Sheets + Drive | `write_jobs_to_sheet.py` (Stage 3) |
| `token_gmail.json` | Sheets + Drive + Gmail Send | `send_followups.py` (Stage 6) |

## Steps

### 1. Google Cloud Console
1. Go to https://console.cloud.google.com/
2. Create a new project (or use existing)
3. Enable **Google Sheets API** and **Google Drive API**
4. Enable **Gmail API** (required for Stage 6 follow-up emails):
   - Go to https://console.cloud.google.com/apis/library/gmail.googleapis.com
   - Click "Enable"

### 2. OAuth Credentials
1. Go to APIs & Services > Credentials
2. Click "Create Credentials" > "OAuth client ID"
3. Application type: **Desktop app**
4. Download the JSON file
5. Save as `credentials.json` in project root (`c:\Users\simon\Desktop\DOE AI Agent\credentials.json`)

### 3. First Run — Sheets Token
1. Run `python execution/write_jobs_to_sheet.py`
2. A browser window opens for Google OAuth consent
3. Grant access to Google Sheets and Drive
4. `token.json` is auto-generated in the project root

### 4. First Run — Gmail Token (for Follow-Up Emails)
1. Run `python execution/send_followups.py` (dry run is default, safe)
2. A browser window opens for Google OAuth consent
3. Grant access to Google Sheets, Drive, **and "Send email on your behalf"**
4. `token_gmail.json` is auto-generated in the project root
5. This token is separate from `token.json` — they don't interfere

### 5. Verify
- `credentials.json` exists in project root
- `token.json` exists after first Sheets run
- `token_gmail.json` exists after first follow-up run
- All three are in `.gitignore` (already configured)

## Notes
- Tokens expire periodically; scripts auto-refresh them
- If token refresh fails, delete the relevant `token*.json` and re-run to re-authenticate
- Gmail token requires Gmail API to be enabled in Cloud Console (Step 1.4)
- The Gmail token has broader scopes (includes Sheets+Drive+Gmail Send)
- DRY RUN mode in `send_followups.py` does NOT send emails — safe for initial token setup
