import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { ColorSchemeProvider } from './contexts/ColorSchemeContext'
import { basePath } from './config/basePath'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename={basePath}>
      <ColorSchemeProvider>
        <App />
      </ColorSchemeProvider>
    </BrowserRouter>
  </React.StrictMode>,
)
