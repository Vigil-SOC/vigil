import React, { createContext, useContext, useState, useEffect } from 'react'
import { configApi } from '../services/api'

interface ColorSchemeContextType {
  scheme: 'light' | 'dark'
  toggleScheme: () => void
  setScheme: (scheme: 'light' | 'dark') => void
}

const ColorSchemeContext = createContext<ColorSchemeContextType | undefined>(undefined)

export const useColorScheme = () => {
  const context = useContext(ColorSchemeContext)
  if (!context) throw new Error('useColorScheme must be used within ColorSchemeProvider')
  return context
}

export const ColorSchemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [scheme, setSchemeState] = useState<'light' | 'dark'>('dark')

  useEffect(() => {
    configApi.getTheme()
      .then(res => res.data.theme && setSchemeState(res.data.theme))
      .catch(() => {})
  }, [])

  const setScheme = (next: 'light' | 'dark') => {
    setSchemeState(next)
    configApi.setTheme(next).catch(() => {})
  }

  const toggleScheme = () => setScheme(scheme === 'light' ? 'dark' : 'light')

  return (
    <ColorSchemeContext.Provider value={{ scheme, toggleScheme, setScheme }}>
      {children}
    </ColorSchemeContext.Provider>
  )
}
