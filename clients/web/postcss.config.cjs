// Tailwind (preflight off — see tailwind.config.cjs) + autoprefixer.
// Tailwind only acts on files containing @tailwind directives;
// all other CSS in the app passes through untouched (autoprefixer aside).
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
