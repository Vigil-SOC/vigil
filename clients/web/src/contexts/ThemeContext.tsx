import React, { createContext, useContext, useState, useEffect } from 'react'
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

  return (
    <ThemeContext.Provider value={{ mode, toggleTheme, setMode }}>
      {children}
    </ThemeContext.Provider>
  )
}
