var __assign = (this && this.__assign) || function () {
    __assign = Object.assign || function(t) {
        for (var s, i = 1, n = arguments.length; i < n; i++) {
            s = arguments[i];
            for (var p in s) if (Object.prototype.hasOwnProperty.call(s, p))
                t[p] = s[p];
        }
        return t;
    };
    return __assign.apply(this, arguments);
};
import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { parseAllowedHosts } from './viteAllowedHosts';
// Get __dirname equivalent in ES modules
var __filename = fileURLToPath(import.meta.url);
var __dirname = dirname(__filename);
// https://vitejs.dev/config/
export default defineConfig(function (_a) {
    var _b;
    var _c;
    var mode = _a.mode;
    // Load env from repo root (two levels up: clients/web → repo root)
    var env = loadEnv(mode, resolve(__dirname, '..', '..'), '');
    // Frontend auth bypass. An explicit VITE_DEV_MODE wins (the desktop app sets
    // it false so its login/bootstrap flow is real); otherwise inherit root .env
    // DEV_MODE for terminal dev. loadEnv('' prefix) also surfaces process.env, so
    // read that first for the explicit override.
    var explicit = (_c = process.env.VITE_DEV_MODE) !== null && _c !== void 0 ? _c : env.VITE_DEV_MODE;
    var devMode = explicit === 'true' || explicit === 'false'
        ? explicit
        : env.DEV_MODE === 'true'
            ? 'true'
            : 'false';
    // Context path (sub-path) the app is served under. Empty = root. In prod the
    // backend injects <meta name="vigil-base-path"> into index.html and the bundle
    // uses relative asset URLs (base './'); in dev we set base to the context
    // path and inject the same meta tag so basePath.ts resolves identically.
    var contextPath = process.env.VIGIL_CONTEXT_PATH || env.VIGIL_CONTEXT_PATH || '';
    var isDev = mode === 'development';
    var base = isDev && contextPath ? "".concat(contextPath, "/") : './';
    // Injected as a runtime <meta> so the dev SPA's trust gate reads the same
    // origins the backend uses for the CSP + SSRF guard (mirrors base-path).
    var extensionAllowlist = process.env.EXTENSION_CONNECTOR_ALLOWLIST || env.EXTENSION_CONNECTOR_ALLOWLIST || '';
    // Hostnames the dev server accepts besides localhost and IPs, for a host
    // reached by name or through a reverse proxy. Read from the root .env so it
    // survives upgrades; unset leaves Vite's default in place.
    var allowedHosts = parseAllowedHosts(process.env.VIGIL_ALLOWED_HOSTS || env.VIGIL_ALLOWED_HOSTS);
    return {
        base: base,
        plugins: [
            {
                name: 'inject-runtime-config',
                transformIndexHtml: function (html) {
                    var tags = [];
                    if (contextPath)
                        tags.push("<meta name=\"vigil-base-path\" content=\"".concat(contextPath, "\">"));
                    if (extensionAllowlist)
                        tags.push("<meta name=\"vigil-extension-allowlist\" content=\"".concat(extensionAllowlist, "\">"));
                    if (!tags.length)
                        return html;
                    return html.replace('<head>', "<head>\n    ".concat(tags.join('\n    ')));
                },
            },
            react(),
        ],
        server: __assign(__assign({ 
            // tokens.css lives under docs/, outside this package. Setting allow
            // replaces Vite's default (the web root), so both are named. Not the repo
            // root: that would serve logs/ and backend source to anyone who can reach
            // this port, which VIGIL_ALLOWED_HOSTS makes possible through a proxy.
            fs: {
                allow: [__dirname, resolve(__dirname, '..', '..', 'docs', 'design', 'console', 'tokens')],
            }, port: 6988, host: '127.0.0.1' }, (allowedHosts !== undefined && { allowedHosts: allowedHosts })), { proxy: (_b = {},
                // Dev proxy must match the context-path-prefixed API calls.
                _b["".concat(contextPath, "/api")] = {
                    target: 'http://127.0.0.1:6987', // Use IPv4 explicitly instead of localhost
                    changeOrigin: true,
                },
                _b) }),
        resolve: {
            // Dedupe React so pre-bundled deps share one instance. Without this,
            // react-router-dom gets its own React copy and hook calls throw
            // "Cannot read properties of null (reading 'useRef')".
            dedupe: ['react', 'react-dom'],
        },
        optimizeDeps: {
            // Pre-bundle these in one Rolldown optimizer pass so React lives in ONE
            // shared chunk instead of being duplicated per dep.
            include: [
                'react',
                'react-dom',
                'react-dom/client',
                'react/jsx-runtime',
                'react/jsx-dev-runtime',
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
    };
});
