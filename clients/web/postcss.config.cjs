// Tailwind v4 (preflight off — styles.css imports theme + utilities only)
// + autoprefixer, which still prefixes shell.css, kit.css, tokens.css, and
// the screen stylesheets. Tailwind only rewrites the stylesheet that imports it.
module.exports = {
  plugins: {
    '@tailwindcss/postcss': {},
    autoprefixer: {},
  },
}
