# Default Credentials Initialization

## Purpose

This script ensures that default admin credentials are inserted into PostgreSQL, allowing first-time login to the Vigil SOC system.

## Default Credentials

- **Username**: `admin`
- **Password**: `admin123`
- **Email**: `admin@deeptempo.ai`
- **Role**: Admin (full system access)

⚠️ **IMPORTANT**: Change the default password after first login!

## Usage

### Prerequisites

1. **Start Docker** (if using Docker Desktop)
2. **Start PostgreSQL**:
   ```bash
   cd /Users/mando222/Github/ai-opensoc
   ./scripts/start_database.sh
   ```

### Run the Initialization Script

```bash
# Option 1: With venv activated
source venv/bin/activate
python3 scripts/init_default_credentials.py

# Option 2: Direct execution
./scripts/init_default_credentials.py
```

### Expected Output

```
==================================================
Vigil SOC - Default Credentials Setup
==================================================
✓ Database connection established
✓ Roles table ready
✓ Users table ready
✓ Default roles inserted (5 roles)
✓ Default admin user created

Verifying authentication...
✓ Authentication verified

==================================================
✓ Default Credentials Ready!
==================================================
Username: admin
Password: admin123
Email:    admin@deeptempo.ai
Role:     Admin (full system access)

⚠️  IMPORTANT: Change the default password after first login!

Available roles:
  - Admin: Full system access
  - Manager: User management and all integrations
  - Senior Analyst: Full analyst access plus approval rights
  - Analyst: Full access to findings and cases, limited integrations
  - Viewer: Read-only access to findings and cases
```

## What It Does

The script:

1. ✅ Connects to PostgreSQL database
2. ✅ Creates `roles` and `users` tables if they don't exist
3. ✅ Inserts 5 default roles with appropriate permissions:
   - Viewer
   - Analyst
   - Senior Analyst
   - Manager
   - Admin
4. ✅ Creates default admin user with password hash
5. ✅ Verifies authentication works
6. ✅ Is idempotent (safe to run multiple times)

## Troubleshooting

### "Database connection failed"

**Problem**: PostgreSQL is not running

**Solution**: 
```bash
# Start Docker Desktop first, then:
./scripts/start_database.sh
```

### "Authentication verification failed"

**Problem**: User was created but login doesn't work

**Solution**: Check password hash or recreate user:
```sql
-- Connect to database
docker exec -it deeptempo-postgres psql -U deeptempo -d deeptempo_soc

-- Delete existing admin user
DELETE FROM users WHERE username = 'admin';

-- Run the script again
```

### Script runs but login still fails on web UI

**Problem**: Frontend may be caching credentials or backend isn't running

**Solution**:
1. Clear browser cache/cookies
2. Start the backend:
   ```bash
   ./start_web.sh
   ```
3. Check backend logs for errors

## When to Use This Script

Run this script when:

- ✅ First time setting up the system
- ✅ After resetting the database
- ✅ Docker volume was deleted and recreated
- ✅ Login with admin/admin123 doesn't work
- ✅ You see "Login failed. Please check your credentials." error

## Files Modified

- Creates/updates `roles` table
- Creates/updates `users` table
- Inserts default system roles
- Inserts default admin user

All operations use `ON CONFLICT DO NOTHING` to ensure idempotency.

