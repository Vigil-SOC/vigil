import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { CssBaseline } from '@mui/material'
import App from './App'
import { ThemeProvider } from './contexts/ThemeContext'
import { NotificationProvider } from './contexts/NotificationContext'
import { basePath } from './config/basePath'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename={basePath}>
      <ThemeProvider>
        <NotificationProvider>
          <CssBaseline />
          <App />
        </NotificationProvider>
      </ThemeProvider>
    </BrowserRouter>
  </React.StrictMode>,
)

