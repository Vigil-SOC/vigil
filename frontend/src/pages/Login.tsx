/**
 * Login Page - User authentication interface.
 * 
 * Provides login form with optional MFA support.
 */

import React, { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Box,
  Paper,
  TextField,
  Button,
  Typography,
  Alert,
  CircularProgress,
  InputAdornment,
  IconButton,
} from '@mui/material';
import {
  Visibility,
  VisibilityOff,
  Security as SecurityIcon,
  ArrowBack,
} from '@mui/icons-material';
import { useAuth } from '../contexts/AuthContext';

export default function Login() {
  const navigate = useNavigate();
  const { login } = useAuth();

  const [usernameOrEmail, setUsernameOrEmail] = useState('');
  const [password, setPassword] = useState('');
  const [mfaCode, setMfaCode] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [showMfa, setShowMfa] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const mfaInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (showMfa) {
      setTimeout(() => mfaInputRef.current?.focus(), 100);
    }
  }, [showMfa]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      await login(usernameOrEmail, password, showMfa ? mfaCode : undefined);
      navigate('/');
    } catch (err: any) {
      if (err.message === 'MFA_REQUIRED') {
        setShowMfa(true);
        setMfaCode('');
        setError('Please enter your 2FA code to continue');
      } else {
        setError(err.response?.data?.detail || 'Login failed. Please check your credentials.');
      }
    } finally {
      setLoading(false);
    }
  };

  const handleBackToCredentials = () => {
    setShowMfa(false);
    setMfaCode('');
    setError('');
  };

  return (
    <Box
      sx={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
        padding: 2,
      }}
    >
      <Paper
        elevation={24}
        sx={{
          padding: 4,
          maxWidth: 400,
          width: '100%',
          borderRadius: 2,
        }}
      >
        {/* Logo/Title */}
        <Box sx={{ textAlign: 'center', mb: 3 }}>
          <SecurityIcon sx={{ fontSize: 48, color: 'primary.main', mb: 1 }} />
          <Typography variant="h4" component="h1" gutterBottom fontWeight="bold">
            Vigil SOC
          </Typography>
          <Typography variant="body2" color="text.secondary">
            {showMfa ? 'Two-factor authentication required' : 'Sign in to your account'}
          </Typography>
        </Box>

        {/* Error Alert */}
        {error && (
          <Alert severity={showMfa ? 'info' : 'error'} sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}

        {/* Login Form */}
        <form onSubmit={handleSubmit}>
          {!showMfa ? (
            <>
              <TextField
                fullWidth
                label="Username or Email"
                variant="outlined"
                margin="normal"
                value={usernameOrEmail}
                onChange={(e) => setUsernameOrEmail(e.target.value)}
                required
                autoFocus
                disabled={loading}
              />

              <TextField
                fullWidth
                label="Password"
                type={showPassword ? 'text' : 'password'}
                variant="outlined"
                margin="normal"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                disabled={loading}
                InputProps={{
                  endAdornment: (
                    <InputAdornment position="end">
                      <IconButton
                        onClick={() => setShowPassword(!showPassword)}
                        edge="end"
                      >
                        {showPassword ? <VisibilityOff /> : <Visibility />}
                      </IconButton>
                    </InputAdornment>
                  ),
                }}
              />
            </>
          ) : (
            <>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                Signing in as <strong>{usernameOrEmail}</strong>
              </Typography>
              <TextField
                fullWidth
                label="2FA Code"
                variant="outlined"
                margin="normal"
                value={mfaCode}
                onChange={(e) => {
                  const val = e.target.value.replace(/\D/g, '');
                  if (val.length <= 6) setMfaCode(val);
                }}
                required
                disabled={loading}
                placeholder="000000"
                inputRef={mfaInputRef}
                inputProps={{
                  maxLength: 6,
                  inputMode: 'numeric',
                  autoComplete: 'one-time-code',
                }}
                helperText="Enter the 6-digit code from your authenticator app"
              />
              <Button
                size="small"
                startIcon={<ArrowBack />}
                onClick={handleBackToCredentials}
                disabled={loading}
                sx={{ mt: 1 }}
              >
                Back to login
              </Button>
            </>
          )}

          <Button
            type="submit"
            fullWidth
            variant="contained"
            size="large"
            disabled={loading || (showMfa && mfaCode.length !== 6)}
            sx={{ mt: 3, mb: 2, py: 1.5 }}
          >
            {loading ? (
              <CircularProgress size={24} color="inherit" />
            ) : showMfa ? (
              'Verify & Sign In'
            ) : (
              'Sign In'
            )}
          </Button>
        </form>

        {/* Footer */}
        <Box sx={{ mt: 3, textAlign: 'center' }}>
          <Typography variant="caption" color="text.secondary">
            Default credentials: admin / admin123
          </Typography>
        </Box>
      </Paper>
    </Box>
  );
}

