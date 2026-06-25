import React, { createContext, useContext, useState, useEffect, useMemo } from 'react'
import { ThemeProvider as MuiThemeProvider, CssBaseline } from '@mui/material'
import { createM3Theme } from '../theme'
import { configApi } from '../services/api'

interface ThemeContextType {
  mode: 'light' | 'dark'
  toggleTheme: () => void
  setMode: (mode: 'light' | 'dark') => void
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined)

export const useTheme = () => {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('useTheme must be used within ThemeProvider')
  return context
}

export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [mode, setModeState] = useState<'light' | 'dark'>('dark')

  useEffect(() => {
    configApi.getTheme()
      .then(res => res.data.theme && setModeState(res.data.theme))
      .catch(() => {})
  }, [])

  const setMode = (newMode: 'light' | 'dark') => {
    setModeState(newMode)
    configApi.setTheme(newMode).catch(() => {})
  }

  const toggleTheme = () => setMode(mode === 'light' ? 'dark' : 'light')

  const theme = useMemo(() => createM3Theme(mode), [mode])

  return (
    <ThemeContext.Provider value={{ mode, toggleTheme, setMode }}>
      <MuiThemeProvider theme={theme}>
        <CssBaseline />
        {children}
      </MuiThemeProvider>
    </ThemeContext.Provider>
  )
}
