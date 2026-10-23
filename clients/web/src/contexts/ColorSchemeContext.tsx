import React, { createContext, useContext, useState, useEffect } from 'react'
import { configApi } from '../services/api'

type Scheme = 'light' | 'dark'

const COLOR_SCHEME_KEY = 'vigil.colorScheme'

function readSavedScheme(): Scheme | null {
  try {
    const saved = localStorage.getItem(COLOR_SCHEME_KEY)
    return saved === 'light' || saved === 'dark' ? saved : null
  } catch {
    return null
  }
}

interface ColorSchemeContextType {
  scheme: Scheme
  setScheme: (scheme: Scheme) => void
}

const ColorSchemeContext = createContext<ColorSchemeContextType | undefined>(undefined)

export const useColorScheme = () => {
  const context = useContext(ColorSchemeContext)
  if (!context) throw new Error('useColorScheme must be used within ColorSchemeProvider')
  return context
}

export const ColorSchemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [scheme, setSchemeState] = useState<Scheme>(() => readSavedScheme() ?? 'dark')

  useEffect(() => {
    if (readSavedScheme()) return
    configApi.getTheme()
      .then(res => !readSavedScheme() && res.data.theme && setSchemeState(res.data.theme))
      .catch(() => {})
  }, [])

  const setScheme = (next: Scheme) => {
    setSchemeState(next)
    try {
      localStorage.setItem(COLOR_SCHEME_KEY, next)
    } catch {
      /* private mode */
    }
  }

  return (
    <ColorSchemeContext.Provider value={{ scheme, setScheme }}>
      {children}
    </ColorSchemeContext.Provider>
  )
}
