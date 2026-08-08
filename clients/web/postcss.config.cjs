// Tailwind (scoped to src/redesign via tailwind.config.cjs, preflight off) +
// autoprefixer. Tailwind only acts on files containing @tailwind directives;
// all other CSS in the app passes through untouched (autoprefixer aside).
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
