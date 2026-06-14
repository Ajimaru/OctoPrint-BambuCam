// Flat ESLint config for the plugin frontend code.
// Globals are declared inline because the repo has no node_modules of its own
// (the pre-commit eslint hook runs in an isolated environment).
export default [
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
