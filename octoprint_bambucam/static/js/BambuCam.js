/**
 * View model for BambuCam.
 *
 * Binds the settings dialog (status panel, connection test, restart button)
 * and the webcam tab (MJPEG `<img>` with the computed stream URL).
 *
 * @module BambuCam
 * @author Ajimaru
 * @license AGPL-3.0-or-later
 */
$(function () {
    /**
     * Knockout view model bound to the BambuCam settings dialog and webcam tab.
     *
     * @class BambucamViewModel
     * @param {Array} parameters - OctoPrint-injected dependencies:
     *   `[settingsViewModel, loginStateViewModel]`.
     */
    function BambucamViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];
        self.loginState = parameters[1];

        // populated in onBeforeBinding, when the settings tree is available
        self.settings = undefined;

        // settings dialog status panel
        self.testing = ko.observable(false);
        self.testResult = ko.observable("");
        self.daemonRunning = ko.observable(false);
        self.daemonPid = ko.observable("-");
        self.encodeFps = ko.observable("-");
        self.sessionCount = ko.observable("-");
        self.lastError = ko.observable("");
        self._statusTimer = undefined;

        // diagnostics (/?info)
        self.fetchingInfo = ko.observable(false);
        self.infoText = ko.observable("");
        self.infoError = ko.observable("");

        // webcam tab
        self.streamLoaded = ko.observable(false);
        self.streamError = ko.observable(false);
        self._streamRetryTimer = undefined;

        // ── stream URL ────────────────────────────────────────────────────

        /**
         * Compute the stream URL for the browser `<img>`.
         *
         * Returns `stream_url_override` if set; otherwise rebuilds the loopback
         * URL against the host the browser is currently talking to.
         *
         * @memberof BambucamViewModel
         * @returns {string} The MJPEG stream URL.
         */
        self.streamUrl = function () {
            var plugin = self.settings.plugins.bambucam;
            var override = plugin.stream_url_override();
            if (override) {
                return override;
            }
            // the daemon runs on the OctoPrint host; replace the loopback host
            // with whatever host the browser is currently talking to
            return (
                location.protocol +
                "//" +
                location.hostname +
                ":" +
                plugin.port() +
                "/?stream"
            );
        };

        // host that the configured bind address resolves to from a browser:
        // 0.0.0.0 means "listen on all interfaces" → reachable via the host
        // the browser already talks to; 127.0.0.1 stays loopback (OctoPrint host
        // only). This mirrors what the bind_address dropdown actually configures.
        /**
         * Resolve the host the configured bind address maps to from a browser.
         *
         * `0.0.0.0` (all interfaces) → the current browser host; anything else
         * stays loopback (`127.0.0.1`, OctoPrint host only).
         *
         * @memberof BambucamViewModel
         * @returns {string} `location.hostname` or `"127.0.0.1"`.
         */
        self._configuredHost = function () {
            var plugin = self.settings.plugins.bambucam;
            return plugin.bind_address() === "0.0.0.0"
                ? location.hostname
                : "127.0.0.1";
        };

        /**
         * Build a daemon URL for the given action against the configured host.
         *
         * @memberof BambucamViewModel
         * @param {string} action - Query action, e.g. `"stream"` or `"snapshot"`.
         * @returns {string} The fully qualified URL.
         */
        self._configuredUrl = function (action) {
            var plugin = self.settings.plugins.bambucam;
            return (
                location.protocol +
                "//" +
                self._configuredHost() +
                ":" +
                plugin.port() +
                "/?" +
                action
            );
        };

        self.snapshotUrlDisplay = ko.pureComputed(function () {
            return self._configuredUrl("snapshot");
        });

        self.streamUrlDisplay = ko.pureComputed(function () {
            return self._configuredUrl("stream");
        });

        /**
         * Copy text to the clipboard, falling back to a hidden input + execCommand.
         *
         * @memberof BambucamViewModel
         * @param {string} text - The text to copy.
         */
        self.copyToClipboard = function (text) {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text);
            } else {
                var tmp = $("<input>");
                $("body").append(tmp);
                tmp.val(text).select();
                document.execCommand("copy");
                tmp.remove();
            }
        };

        self.copySnapshotUrl = function () {
            self.copyToClipboard(self.snapshotUrlDisplay());
        };

        self.copyStreamUrl = function () {
            self.copyToClipboard(self.streamUrlDisplay());
        };

        /**
         * (Re)load the MJPEG `<img>` source with a cache-busting query param.
         *
         * @memberof BambucamViewModel
         */
        self._loadStream = function () {
            var img = $("#bambucam_stream");
            if (!img.length) return;
            self.streamError(false);
            img.attr("src", self.streamUrl() + "&cb=" + Date.now());
        };

        /**
         * `<img>` load handler — mark the stream as live.
         *
         * @memberof BambucamViewModel
         */
        self.onStreamLoaded = function () {
            self.streamLoaded(true);
            self.streamError(false);
        };

        /**
         * `<img>` error handler — flag the error and retry in 10 s (the daemon
         * may still be starting up).
         *
         * @memberof BambucamViewModel
         */
        self.onStreamErrored = function () {
            self.streamLoaded(false);
            self.streamError(true);
            // retry: the daemon may still be starting up
            clearTimeout(self._streamRetryTimer);
            self._streamRetryTimer = setTimeout(self._loadStream, 10000);
        };

        // ── settings dialog ───────────────────────────────────────────────

        /**
         * Run the printer connection test via the simple API and surface the
         * result in the status panel.
         *
         * @memberof BambucamViewModel
         */
        self.testConnection = function () {
            var plugin = self.settings.plugins.bambucam;
            self.testing(true);
            self.testResult("");
            OctoPrint.simpleApiCommand("bambucam", "test_connection", {
                hostname: plugin.hostname(),
                access_code: plugin.access_code(),
            })
                .done(function (response) {
                    self.testResult(response.ok ? "ok" : response.reason);
                })
                .fail(function () {
                    self.testResult("error");
                })
                .always(function () {
                    self.testing(false);
                });
        };

        /**
         * Fetch the daemon's `/?info` diagnostics (access code redacted) and
         * pretty-print the JSON, or show an error reason.
         *
         * @memberof BambucamViewModel
         */
        self.fetchInfo = function () {
            self.fetchingInfo(true);
            self.infoError("");
            self.infoText("");
            OctoPrint.simpleApiCommand("bambucam", "fetch_info", {})
                .done(function (response) {
                    if (response.ok) {
                        self.infoText(JSON.stringify(response.info, null, 2));
                    } else {
                        self.infoError(
                            response.reason === "unreachable"
                                ? gettext(
                                      "Stream server not reachable. Is it running?",
                                  )
                                : response.reason,
                        );
                    }
                })
                .fail(function () {
                    self.infoError(gettext("Request failed."));
                })
                .always(function () {
                    self.fetchingInfo(false);
                });
        };

        /**
         * Request a daemon restart and refresh the status panel afterwards.
         *
         * @memberof BambucamViewModel
         */
        self.restartDaemon = function () {
            OctoPrint.simpleApiCommand("bambucam", "restart", {}).done(
                function () {
                    self._fetchStatus();
                },
            );
        };

        /**
         * Poll the daemon status endpoint and update the observable panel
         * fields (running, PID, encode FPS, session count, last error).
         *
         * @memberof BambucamViewModel
         */
        self._fetchStatus = function () {
            OctoPrint.simpleApiGet("bambucam").done(function (status) {
                self.daemonRunning(status.running);
                self.daemonPid(status.pid || "-");
                self.lastError(status.last_error || "");
                if (status.info && status.info.stats) {
                    self.encodeFps(status.info.stats.encodeFps);
                    self.sessionCount(status.info.stats.sessionCount);
                } else {
                    self.encodeFps("-");
                    self.sessionCount("-");
                }
            });
        };

        self.onSettingsShown = function () {
            self._fetchStatus();
            self._statusTimer = setInterval(self._fetchStatus, 10000);
        };

        self.onSettingsHidden = function () {
            clearInterval(self._statusTimer);
        };

        self.onSettingsSaved = function () {
            // daemon may have been restarted with a new port → reconnect stream
            setTimeout(self._loadStream, 2000);
        };

        // ── lifecycle ─────────────────────────────────────────────────────

        self.onBeforeBinding = function () {
            self.settings = self.settingsViewModel.settings;
        };

        self.onStartupComplete = function () {
            self._loadStream();
        };

        /**
         * Data-updater handler for `daemon_state` push messages: shows an
         * error PNotify on `gave_up`, an info PNotify on `offline`, reloads the
         * stream on `started`, and refreshes the status panel.
         *
         * @memberof BambucamViewModel
         * @param {string} plugin - Originating plugin identifier.
         * @param {Object} data - Message payload (`{type, state, detail}`).
         */
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "bambucam") return;
            if (data.type !== "daemon_state") return;

            if (data.state === "gave_up") {
                self.lastError(data.detail.error);
                new PNotify({
                    title: "BambuCam",
                    text: data.detail.error,
                    type: "error",
                    hide: false,
                });
            } else if (data.state === "offline") {
                // Printer unreachable (e.g. powered off): expected, not an
                // error. The daemon keeps reconnecting on its own and the
                // stream shows a "Printer Offline" frame, so just inform.
                self.lastError("");
                new PNotify({
                    title: "BambuCam",
                    text: gettext(
                        "Printer is offline — reconnecting automatically.",
                    ),
                    type: "info",
                    hide: true,
                });
            } else if (data.state === "started") {
                self.lastError("");
                setTimeout(self._loadStream, 2000);
            }
            self._fetchStatus();
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: BambucamViewModel,
        dependencies: ["settingsViewModel", "loginStateViewModel"],
        elements: ["#settings_plugin_bambucam", "#bambucam_webcam_container"],
    });
});
