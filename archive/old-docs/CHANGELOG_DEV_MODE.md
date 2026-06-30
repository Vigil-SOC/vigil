# DEV_MODE Configuration Simplification - Changelog

## Summary

Simplified DEV_MODE configuration to use a **single environment variable** in the root `.env` file that controls **both backend and frontend** authentication bypass behavior.

## What Changed?

### Before (v1.x)
- **Two separate configurations required:**
  - Backend: `DEV_MODE=true` in root `.env` file
  - Frontend: `VITE_DEV_MODE=true` in `frontend/.env.development` file
- Users had to maintain two separate environment files
- Easy to forget to configure one or the other
- Inconsistent dev mode state between backend and frontend

### After
- **One unified configuration:**
  - Set `DEV_MODE=true` in root `.env` file
  - Both backend AND frontend automatically use it
- No need for `frontend/.env.development` file
- Simplified setup and maintenance
- Consistent dev mode state across entire application

## Technical Implementation

### Frontend Configuration (`frontend/vite.config.ts`)
- Modified Vite config to load environment variables from **parent directory** (project root)
- Automatically reads `DEV_MODE` from root `.env` file
- Exposes it to frontend as `VITE_DEV_MODE` via `define` configuration
- Works transparently - no code changes needed in React components

### Setup Script (`setup_dev.sh`)
- Removed frontend `.env.development` file creation step
- Now only copies root `.env` file
- Added informational message that frontend will use DEV_MODE from root

## Files Modified

### Configuration Files
1. **`frontend/vite.config.ts`** - Load env from parent directory, expose as VITE_DEV_MODE
2. **`env.example`** - Updated comments to clarify unified configuration
3. **`frontend/env.development.example`** - Updated to indicate file is now optional

### Scripts
4. **`setup_dev.sh`** - Removed frontend env file creation
5. **`start_web.sh`** - No changes needed (already loads .env)
6. **`start_daemon.sh`** - No changes needed (already loads .env)

### Documentation
7. **`QUICKSTART.md`** - Simplified DEV_MODE section
8. **`DEV_MODE.md`** - Complete rewrite of configuration instructions
9. **`README.md`** - Removed frontend env copy instruction

## Migration Guide

### For Existing Installations

If you already have `frontend/.env.development` file:

**Option 1: Remove it (recommended)**
```bash
# Remove the frontend-specific file
rm frontend/.env.development

# Make sure DEV_MODE is set in root .env
echo "DEV_MODE=true" >> .env

# Restart frontend (it will now read from root .env)
cd frontend && npm run dev
```

**Option 2: Keep it (for manual overrides)**
```bash
# Keep frontend/.env.development if you want to manually override
# It will take precedence over root .env DEV_MODE
# But for most users, removing it is simpler
```

### For Fresh Installations

Just run:
```bash
./setup_dev.sh
# OR manually:
cp .env.example .env
```

That's it! No frontend-specific configuration needed.

## Benefits

1. **✅ Simpler Setup** - One file to configure instead of two
2. **✅ Less Confusion** - Clear single source of truth for DEV_MODE
3. **✅ Easier Toggling** - Change one setting to affect entire app
4. **✅ Consistent State** - Backend and frontend always in sync
5. **✅ Better DX** - Less friction for new developers
6. **✅ Fewer Errors** - Can't forget to configure one side or the other

## Testing

To test the new configuration:

```bash
# 1. Set DEV_MODE in root .env
echo "DEV_MODE=true" > .env

# 2. Start services
./start_web.sh

# 3. Check both work:
# - Backend: Check logs for "DEV_MODE is ENABLED" warning
# - Frontend: Check browser console for "DEV_MODE is ENABLED" warning

# 4. Verify no login required
# - Open http://localhost:6988
# - Should automatically be logged in as dev-user
```

## Backwards Compatibility

**Frontend `.env.development` files are still supported** for manual overrides if needed.

If both exist:
- `frontend/.env.development` takes precedence (Vite's default behavior)
- Root `.env` is used as fallback

But for most users, the frontend `.env.development` file is no longer necessary.

## Questions?

See the updated [DEV_MODE.md](DEV_MODE.md) for full documentation.

