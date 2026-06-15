// Flat ESLint config for the plugin frontend code.
// Globals are declared inline because the repo has no node_modules of its own
// (the pre-commit eslint hook runs in an isolated environment).
export default [
  {
    // Mirror of .gitignore: ESLint flat config does not read .gitignore, so the
    // same build/cache/tool paths are ignored here. A standalone `ignores` block
    // (no other keys) is treated as global ignores. Keep in sync with .gitignore.
    ignores: [
      ".venv*/**",
      ".idea/**",
      ".ruff_cache/**",
      ".pytest_cache/**",
      ".ideas/**",
      ".vscode/**",
      ".development/**",
      ".logs/**",
      ".cache/**",
      ".codacy/**",
      "build/**",
      "dist/**",
      "site/**",
      "node_modules/**",
      "**/*.egg-info/**",
    ],
  },
  {
    files: ["octoprint_bambucam/static/js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: "script",
      globals: {
        // browser
        window: "readonly",
        document: "readonly",
        location: "readonly",
        navigator: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        // jQuery / Knockout / OctoPrint frontend
        $: "readonly",
        jQuery: "readonly",
        ko: "readonly",
        OctoPrint: "readonly",
        OCTOPRINT_VIEWMODELS: "readonly",
        PNotify: "readonly",
        gettext: "readonly",
        _: "readonly",
      },
    },
    rules: {
      "no-unused-vars": ["warn", { args: "none" }],
      "no-undef": "error",
    },
  },
];
