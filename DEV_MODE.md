# Development Mode - Quick Iteration Guide

## 🚀 What is DEV_MODE?

DEV_MODE is a development-only feature that **completely bypasses authentication** to allow rapid iteration without having to log in repeatedly. 

**⚠️ CRITICAL WARNING**: This should **NEVER** be enabled in staging or production environments!

---

## 🎯 When to Use DEV_MODE

### ✅ Good Use Cases:
- Local frontend development and UI iteration
- Testing new features quickly
- Debugging without authentication barriers
- Running automated tests locally
- Rapid prototyping

### ❌ Never Use For:
- Staging environment
- Production environment
- Security testing
- Authentication flow testing
- Demos to stakeholders
- Any environment accessible from outside your machine

---

## 🔧 How to Enable DEV_MODE

### 🎉 Quick Setup for Fresh Install

**⚡ EASIEST: Use the setup script**

```bash
./setup_dev.sh
```

This script copies the `.env` file with DEV_MODE enabled by default!

**Manual alternative:**

```bash
# Copy the template (DEV_MODE already set to true)
cp .env.example .env

# Start services - auth will be bypassed!
./start.sh
```

That's it! **One file, one setting** - both backend and frontend will bypass authentication. 🚀

### ✨ New in v2.0: Unified DEV_MODE Configuration

You now only need to set `DEV_MODE=true` in the **root `.env` file**!

The frontend automatically reads this setting - no separate frontend configuration needed. This makes it much simpler to enable/disable dev mode across the entire application.

### Setting DEV_MODE

**Option 1: Environment File (Recommended)**
```bash
# In root .env file
DEV_MODE=true

# Then restart services
./start.sh
# OR
./start.sh --daemon
```

**Option 2: Docker Compose**
```yaml
# In docker-compose.yml
services:
  soc-api:
    environment:
      - DEV_MODE=true
```

**Option 3: Terminal Export**
```bash
export DEV_MODE=true
./start.sh
```

### How It Works

When you set `DEV_MODE=true` in the root `.env` file:

**Backend (FastAPI):**
- Reads `DEV_MODE` directly from environment
- Bypasses JWT authentication
- Returns mock admin user

**Frontend (React + Vite):**
- Vite reads `DEV_MODE` from root `.env` file
- Automatically exposes it as `VITE_DEV_MODE` to the frontend
- Frontend bypasses login UI and uses mock admin user

No separate frontend configuration needed! 🎉

---

## 🧪 Quick Start with DEV_MODE

### 1. Enable Dev Mode

**For Fresh Install** (Recommended):
```bash
# Copy template (DEV_MODE already set to true)
cp .env.example .env
```

**For Existing Setup**:
```bash
# Just add to root .env file - controls both backend and frontend!
echo "DEV_MODE=true" >> .env
```

### 2. Restart Services

```bash
# Restart API
docker-compose restart soc-api

# Restart frontend (if running)
cd frontend
npm run dev
```

### 3. Access Application

```bash
# Open browser
open http://localhost:6988

# You should be automatically "logged in" as dev-user
# No login screen will appear!
```

---

## 🔍 What Happens in DEV_MODE?

### Backend Behavior:

1. **Authentication Bypassed**: `get_current_user()` returns a mock admin user
2. **No JWT Validation**: Token validation is completely skipped
3. **Full Permissions**: ALL permission checks return `True` - complete unrestricted access
4. **Warning Logs**: Server logs will show warnings that DEV_MODE is active

```python
# Normal mode
Authorization: Bearer <valid-jwt-token>  # Required

# Dev mode
Authorization: <anything or nothing>     # Ignored
```

### Frontend Behavior:

1. **Login Skipped**: No redirect to /login
2. **Mock User Loaded**: Automatically logged in as "dev-user"
3. **Full Access**: ALL permission checks return `true` - every feature is accessible
4. **Console Warnings**: Browser console shows DEV_MODE warnings

```javascript
// Mock user automatically set with ALL permissions:
{
  user_id: 'dev-user-id',
  username: 'dev-user',
  email: 'dev@localhost',
  full_name: 'Dev User (Full Admin)',
  role_id: 'role-admin',
  permissions: {
    // ALL system permissions set to true:
    'findings.read': true, 'findings.write': true, 'findings.delete': true,
    'cases.read': true, 'cases.write': true, 'cases.delete': true, 'cases.assign': true,
    'integrations.read': true, 'integrations.write': true,
    'users.read': true, 'users.write': true, 'users.delete': true,
    'settings.read': true, 'settings.write': true,
    'ai_chat.use': true, 'ai_decisions.approve': true,
    // PLUS: hasPermission() returns true for ANY permission check
  }
}
```

---

## 🛡️ Security Considerations

### ⚠️ Never Enable in Production

**Risks if DEV_MODE is enabled in production:**
- **No authentication** - Anyone can access the system
- **Full admin access** - All users have admin permissions
- **Data exposure** - Sensitive data is unprotected
- **Audit trail broken** - All actions appear to be from "dev-user"
- **Compliance violation** - Likely violates security compliance

### ✅ Safety Measures Implemented:

1. **Prominent Warnings**: 
   - Console warnings in both backend and frontend
   - Logs clearly indicate DEV_MODE is active

2. **Environment Separation**:
   - Separate `.env.development` files
   - Not in default `.env.example`

3. **Documentation**:
   - Clear warnings in all documentation
   - This dedicated guide

4. **Code Reviews**:
   - Easy to spot in code reviews (env vars)
   - Grepping for `DEV_MODE` is straightforward

---

## 🐛 Troubleshooting DEV_MODE

### Problem: Still Redirecting to Login

**Check Configuration:**
```bash
# Check if DEV_MODE is set in root .env
cat .env | grep DEV_MODE

# Should show: DEV_MODE=true
```

**Check Backend:**
```bash
# Check if backend sees DEV_MODE
docker-compose exec soc-api env | grep DEV_MODE

# Should show: DEV_MODE=true
```

**Solution:**
```bash
# Make sure DEV_MODE=true is in root .env
echo "DEV_MODE=true" >> .env

# Restart both services
./start.sh
# OR
docker-compose restart soc-api
cd frontend && npm run dev
```

### Problem: API Returns 401 Unauthorized

This means backend DEV_MODE is not enabled.

**Check logs:**
```bash
docker-compose logs soc-api | grep "DEV_MODE"

# Should see: "⚠️  DEV_MODE is ENABLED"
```

**If not showing:**
```bash
# Set in docker-compose.yml or .env
DEV_MODE=true

# Restart
docker-compose restart soc-api
```

### Problem: Frontend Still Shows Login Page

This means DEV_MODE is not being read by the frontend.

**Check console:**
```javascript
// Should see in browser console:
"⚠️  DEV_MODE is ENABLED - Authentication is BYPASSED!"
```

**If not showing:**
```bash
# Make sure DEV_MODE=true is in root .env (not frontend/.env.development)
cat .env | grep DEV_MODE

# If missing, add it
echo "DEV_MODE=true" >> .env

# Restart frontend dev server (it will read from root .env via Vite)
cd frontend
npm run dev
```

---

## 📝 Testing Authentication with DEV_MODE

### Temporarily Disable for Auth Testing

**Simple - Just change one setting:**
```bash
# In root .env file, comment out or set to false
DEV_MODE=false

# Restart services (both backend and frontend will use auth)
./start.sh
# OR
docker-compose restart soc-api

cd frontend
npm run dev
```

Now you can test the actual login flow!

---

## 🔄 Switching Between Modes

### Quick Toggle Script

Create a helper script:

```bash
#!/bin/bash
# toggle_dev_mode.sh

if [ "$1" == "on" ]; then
    echo "Enabling DEV_MODE..."
    # Update or add DEV_MODE=true to .env
    if grep -q "^DEV_MODE=" .env 2>/dev/null; then
        sed -i.bak 's/^DEV_MODE=.*/DEV_MODE=true/' .env
    else
        echo "DEV_MODE=true" >> .env
    fi
    echo "✅ DEV_MODE enabled - restart services"
elif [ "$1" == "off" ]; then
    echo "Disabling DEV_MODE..."
    # Update DEV_MODE to false in .env
    if grep -q "^DEV_MODE=" .env 2>/dev/null; then
        sed -i.bak 's/^DEV_MODE=.*/DEV_MODE=false/' .env
    fi
    echo "✅ DEV_MODE disabled - restart services"
else
    echo "Usage: ./toggle_dev_mode.sh [on|off]"
fi
```

Make executable:
```bash
chmod +x toggle_dev_mode.sh
```

Use it:
```bash
# Enable (controls both backend and frontend!)
./toggle_dev_mode.sh on
./start.sh

# Disable
./toggle_dev_mode.sh off
./start.sh
```

---

## 📊 DEV_MODE vs Normal Mode Comparison

| Feature | Normal Mode | DEV_MODE |
|---------|-------------|----------|
| **Login Required** | ✅ Yes | ❌ No |
| **JWT Validation** | ✅ Enforced | ❌ Bypassed |
| **Permissions Check** | ✅ RBAC enforced | ✅ All granted |
| **User Identity** | ✅ Real users | ⚠️ Mock "dev-user" |
| **Audit Trail** | ✅ Accurate | ⚠️ All as "dev-user" |
| **Security** | ✅ Secure | ❌ INSECURE |
| **Iteration Speed** | 🐢 Slower | 🚀 Faster |
| **Production Ready** | ✅ Yes | ❌ NEVER |

---

## ✅ Best Practices

### DO:
- ✅ Use DEV_MODE for local development only
- ✅ Set DEV_MODE=true in root `.env` file (controls both backend and frontend!)
- ✅ Disable DEV_MODE before committing code
- ✅ Test authentication flows with DEV_MODE off
- ✅ Document when DEV_MODE was used in PRs
- ✅ Keep `.env` file in `.gitignore`

### DON'T:
- ❌ Enable DEV_MODE in staging/production
- ❌ Commit `.env` files with DEV_MODE=true
- ❌ Share DEV_MODE configs outside team
- ❌ Forget DEV_MODE is enabled during demos
- ❌ Use DEV_MODE for security testing
- ❌ Leave DEV_MODE on overnight (someone might access your dev machine)

---

## 🎓 Example Workflows

### Workflow 1: Frontend UI Development

```bash
# 1. Enable DEV_MODE (single setting for both!)
echo "DEV_MODE=true" >> .env

# 2. Start frontend
cd frontend
npm run dev

# 3. Develop UI
# - No login required
# - Instant access to all features
# - Full admin permissions
# - Backend auth also bypassed automatically!

# 4. When done, disable for testing
sed -i 's/DEV_MODE=true/DEV_MODE=false/' .env
npm run dev

# 5. Test actual login flow
```

### Workflow 2: Backend API Development

```bash
# 1. Enable DEV_MODE in .env
echo "DEV_MODE=true" >> .env

# 2. Start API
uvicorn backend.main:app --reload

# 3. Test endpoints with curl (no auth needed)
curl http://localhost:6987/api/cases
# Works without Authorization header!

# 4. When done, disable for testing
sed -i 's/DEV_MODE=true/DEV_MODE=false/' .env
uvicorn backend.main:app --reload

# 5. Test with real auth
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:6987/api/cases
```

### Workflow 3: Full Stack Development

```bash
# 1. Enable DEV_MODE (one setting controls everything!)
echo "DEV_MODE=true" >> .env

# 2. Start all services
./start.sh
# OR
docker-compose up -d
cd frontend && npm run dev

# 3. Develop freely without auth barriers
# - Backend auth bypassed
# - Frontend auth bypassed
# - All from one setting!

# 4. Before committing
sed -i 's/DEV_MODE=true/DEV_MODE=false/' .env

# 5. Test and commit
```

---

## 🔒 Pre-Production Checklist

Before deploying to staging or production, verify:

- [ ] `DEV_MODE` is not set to `true` in `.env` files (or is set to `false`)
- [ ] Docker Compose has no `DEV_MODE=true` in environment section
- [ ] Environment variables verified in deployment config
- [ ] Authentication tested and working (login page appears)
- [ ] RBAC permissions enforced correctly
- [ ] Audit logs showing real users (not "dev-user")
- [ ] No console warnings about DEV_MODE in browser
- [ ] No server logs showing DEV_MODE warnings
- [ ] Security scan completed
- [ ] Code review approved

---

## 📞 Support

If you encounter issues with DEV_MODE:

1. Check this guide first
2. Verify environment variables are set correctly
3. Check console/logs for DEV_MODE warnings
4. Try restarting services
5. Check `.env.development` vs `.env` files

---

## 🎉 Summary

DEV_MODE is a powerful tool for rapid development, but use it responsibly:

- ✅ **Faster Iteration**: No login required
- ✅ **Full Access**: All permissions granted
- ⚠️ **Local Only**: Never in production
- ⚠️ **No Security**: Authentication completely bypassed

**Happy developing! 🚀**

---

*Last Updated: January 20, 2026*  
*Version: 2.0.0*

