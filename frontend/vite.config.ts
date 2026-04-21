import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'url'
import { dirname, resolve } from 'path'

// Get __dirname equivalent in ES modules
const __filename = fileURLToPath(import.meta.url)
const __dirname = dirname(__filename)

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  // Load env from root directory (parent of frontend)
  const env = loadEnv(mode, resolve(__dirname, '..'), '')
  
  // If DEV_MODE is set in root .env, pass it to frontend as VITE_DEV_MODE
  const devMode = env.DEV_MODE === 'true' ? 'true' : 'false'
  
  return {
    plugins: [react()],
    server: {
      port: 6988,
      host: '127.0.0.1', // Use IPv4 explicitly
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:6987', // Use IPv4 explicitly instead of localhost
          changeOrigin: true,
        },
      },
    },
    resolve: {
      // Dedupe React + emotion so pre-bundled deps share one instance.
      // Without this, react-router-dom gets its own React copy and hook
      // calls throw "Cannot read properties of null (reading 'useRef')".
      dedupe: [
        'react',
        'react-dom',
        'react-is',
        'react-transition-group',
        '@emotion/react',
        '@emotion/styled',
      ],
    },
    optimizeDeps: {
      // Force all MUI + emotion + react-transition-group into a single
      // esbuild optimize pass so React lives in ONE shared chunk instead
      // of being duplicated per dep. Also works around the esbuild 0.21.x
      // cross-chunk splitting bug ("Export 'import_react3' is not defined")
      // because all interdependent modules are discovered together.
      include: [
        'react',
        'react-dom',
        'react-dom/client',
        'react/jsx-runtime',
        'react/jsx-dev-runtime',
        'react-transition-group',
        '@emotion/react',
        '@emotion/styled',
        '@mui/material',
        '@mui/material/styles',
        '@mui/icons-material',
        '@mui/lab',
        'react-router-dom',
      ],
    },
    build: {
      outDir: 'build',
    },
    define: {
      // Make DEV_MODE from root .env available as VITE_DEV_MODE in frontend
      'import.meta.env.VITE_DEV_MODE': JSON.stringify(devMode),
    },
  }
})

