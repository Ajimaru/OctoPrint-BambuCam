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
        self.printerState = parameters[2];

        // populated in onBeforeBinding, when the settings tree is available
        self.settings = undefined;

        self.testing = ko.observable(false);
        self.testResult = ko.observable("");
        self.daemonRunning = ko.observable(false);
        self.daemonPid = ko.observable("-");
        self.encodeFps = ko.observable("-");
        self.sessionCount = ko.observable("-");
        self.lastError = ko.observable("");
        self._statusTimer = undefined;

        self.fetchingInfo = ko.observable(false);
        self.infoText = ko.observable("");
        self.infoError = ko.observable("");

        // Bambu Connector auto-config: probed when the settings dialog opens.
        // `connectorAvailable` gates the "Auto" option; the hostname/access-code
        // fields show the connector's values (read-only) while in auto mode.
        self.connectorAvailable = ko.observable(false);
        self.connectorHostname = ko.observable("");
        // we never receive the plaintext access code from the server; show a
        // fixed mask in the disabled field so it reads as "configured"
        self.connectorAccessCodeMask = ko.observable("");
        // true when config_source is "auto" AND the connector is usable; only
        // then are the manual fields swapped out and disabled
        self.connectionFieldsAuto = ko.pureComputed(function () {
            if (!self.settings) return false;
            var src = self.settings.plugins.bambucam.config_source();
            return src === "auto" && self.connectorAvailable();
        });

        // ffmpeg availability indicator (Timelapse settings tab). `state` is
        // "ok" (found + runnable), "missing" (not configured / not runnable),
        // or "" (not probed yet); `path` is shown when present.
        self.ffmpegState = ko.observable("");
        self.ffmpegPath = ko.observable("");

        self.streamLoaded = ko.observable(false);
        self.streamError = ko.observable(false);
        self._streamRetryTimer = undefined;

        // Printer LED (light) control over MQTT. Only possible when the server
        // knows the printer serial (supplied by OctoPrint-BambuConnector), so
        // `ledAvailable` gates the overlay button. `ledOn` is optimistic: it
        // flips immediately and reverts if the command fails. `ledBusy`
        // disables the button while a command is in flight.
        self.ledAvailable = ko.observable(false);
        self.ledOn = ko.observable(false);
        self.ledBusy = ko.observable(false);
        // The printer must be reachable for MQTT to work. Tracked from the
        // daemon status / push messages: a powered-off printer makes the daemon
        // go "offline", so we hide the button rather than fire doomed commands.
        self.ledOnline = ko.observable(false);
        // Button shows only when control is possible (serial known) AND the
        // printer is currently online.
        self.ledVisible = ko.pureComputed(function () {
            return self.ledAvailable() && self.ledOnline();
        });
        // Coalesce rapid clicks: remember the last requested state while a
        // command is in flight and only send it once the current one finishes,
        // so we never stack connect/publish cycles on the printer's broker.
        self._ledPending = null;

        self.timelapseFiles = ko.observableArray([]);
        self.timelapseLoading = ko.observable(false);
        self.timelapseReason = ko.observable("");
        self.opRunning = ko.observable(false);
        self.lightboxUrl = ko.observable("");
        // local .avi (downloaded but not yet transcoded) awaiting conversion
        self.localAvi = ko.observableArray([]);
        self.convertRunning = ko.observable(false);
        // move/delete are blocked while a print runs (the server is the
        // authoritative guard; this just disables the buttons up front).
        self.printActive = ko.pureComputed(function () {
            if (!self.printerState) return false;
            return (
                self.printerState.isPrinting() || self.printerState.isPaused()
            );
        });
        self._timelapseLoaded = false;

        // List sort order, mirroring OctoPrint's native Timelapse tab. The
        // table binds to `sortedTimelapseFiles` (view only); selection and the
        // batch actions keep operating on the underlying `timelapseFiles`.
        self.timelapseSort = ko.observable("date_desc");
        self.sortedTimelapseFiles = ko.pureComputed(function () {
            var files = self.timelapseFiles().slice();
            var sort = self.timelapseSort();
            // Date order uses the Bambu name (video_YYYY-MM-DD_HH-MM-SS), which
            // sorts chronologically as a string — same source as the displayed
            // date, so it is reliable even when the FTP server omits mtimes.
            var cmp = {
                name_desc: function (a, b) {
                    return b.name.localeCompare(a.name);
                },
                date_desc: function (a, b) {
                    return b.name.localeCompare(a.name);
                },
                size_desc: function (a, b) {
                    return (b.size || 0) - (a.size || 0);
                },
            }[sort];
            return cmp ? files.sort(cmp) : files;
        });
        self.setTimelapseSort = function (order) {
            self.timelapseSort(order);
        };

        self.selectedRows = ko.pureComputed(function () {
            return self.timelapseFiles().filter(function (f) {
                return f.selected();
            });
        });
        self.selectedNames = ko.pureComputed(function () {
            return self.selectedRows().map(function (f) {
                return f.name;
            });
        });
        self.selectedCount = ko.pureComputed(function () {
            return self.selectedRows().length;
        });
        self.selectedLabel = ko.pureComputed(function () {
            return self.selectedCount() + " " + gettext("selected");
        });
        self.selectAll = ko.computed({
            read: function () {
                var files = self.timelapseFiles();
                return (
                    files.length > 0 &&
                    files.every(function (f) {
                        return f.selected();
                    })
                );
            },
            write: function (value) {
                self.timelapseFiles().forEach(function (f) {
                    f.selected(value);
                });
            },
        });

        /**
         * Derive a human-readable date for a timelapse row.
         *
         * The printer's FTP server often omits the MLSD `modify` fact (and the
         * NLST fallback has none at all), leaving the column empty. Bambu names
         * its timelapses `video_YYYY-MM-DD_HH-MM-SS.avi`, so parse the date from
         * the file name first and fall back to the raw `modify` timestamp
         * (`YYYYMMDDhhmmss`) when the name does not match.
         *
         * @param {string} name - The video file name.
         * @param {string} [raw] - Optional MLSD `modify` timestamp.
         * @returns {string} e.g. `"2026-05-18 17:18"`, or `""` if unknown.
         */
        self._formatDate = function (name, raw) {
            var m = /(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})/.exec(
                name || "",
            );
            if (m) {
                return m[1] + "-" + m[2] + "-" + m[3] + " " + m[4] + ":" + m[5];
            }
            // MLSD modify fact: YYYYMMDDhhmmss (14 digits)
            var d = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})/.exec(raw || "");
            if (d) {
                return d[1] + "-" + d[2] + "-" + d[3] + " " + d[4] + ":" + d[5];
            }
            return raw || "";
        };

        /**
         * Build a Knockout row object for one timelapse file.
         *
         * @param {Object} f - Server file record `{name, size, date, copied}`.
         * @returns {Object} Row with observables used by the table bindings.
         */
        self._makeRow = function (f) {
            // The A1 mini stamps the SD file with a wrong camera-clock date in
            // LAN-only mode. The server sends a real `date` with
            // date_corrected=true when it has one (a copied file's mtime, or
            // the PrintDone time we recorded for this video). When it has
            // neither, date_unreliable=true and we must not show the bogus raw
            // date as if it were real.
            var rawDate = self._formatDate(f.name, f.date);
            var displayDate = f.date_corrected ? f.date : "";
            var row = {
                name: f.name,
                size: f.size,
                date: displayDate,
                dateRaw: rawDate,
                dateCorrected: !!f.date_corrected,
                dateUnreliable: !!f.date_unreliable,
                copied: ko.observable(!!f.copied),
                selected: ko.observable(false),
                progress: ko.observable(-1), // -1 = no op in flight
                rowError: ko.observable(""),
                etaText: ko.observable(""),
                converting: ko.observable(false), // ffmpeg .avi→.mp4 running
                convertProgress: ko.observable(0), // ffmpeg percent
                // local name it was copied to (e.g. transcoded .mp4); seeded
                // from the listing so the "→ name" hint survives a restart
                renamedTo: ko.observable(f.renamed || ""),
                thumbUrl: self._thumbUrl(f.name),
                thumbOk: ko.observable(true),
                // transfer-rate tracking for the ETA (set on progress events)
                _etaStart: 0,
                _etaStartBytes: 0,
            };
            row.sizeText = ko.pureComputed(function () {
                return self._humanSize(row.size);
            });
            return row;
        };

        /**
         * Update a row's ETA text from a download progress event.
         *
         * Computes the transfer rate over the elapsed wall-clock window since
         * the transfer started and projects the remaining bytes onto it.
         *
         * @param {Object} row - The timelapse row being downloaded.
         * @param {Object} data - The progress message (`transferred`, `total`).
         */
        self._updateEta = function (row, data) {
            var now = Date.now();
            var transferred = data.transferred;
            var total = data.total;
            if (!total || transferred == null) {
                row.etaText("");
                return;
            }
            // (re)start the window when a new transfer begins
            if (!row._etaStart || transferred < row._etaStartBytes) {
                row._etaStart = now;
                row._etaStartBytes = transferred;
                return;
            }
            var elapsed = (now - row._etaStart) / 1000;
            var bytes = transferred - row._etaStartBytes;
            if (elapsed < 0.5 || bytes <= 0) return; // too early to estimate
            var rate = bytes / elapsed; // bytes/s
            var remaining = (total - transferred) / rate; // seconds
            row.etaText(self._formatEta(remaining));
        };

        /**
         * Format a duration in seconds as a short ETA string.
         *
         * @param {number} seconds - Remaining seconds.
         * @returns {string} e.g. `"~2m 05s"`, `"~12s"`.
         */
        self._formatEta = function (seconds) {
            if (!isFinite(seconds) || seconds < 0) return "";
            seconds = Math.round(seconds);
            if (seconds < 60) return "~" + seconds + "s";
            var m = Math.floor(seconds / 60);
            var s = seconds % 60;
            if (m < 60) {
                return "~" + m + "m " + (s < 10 ? "0" : "") + s + "s";
            }
            var h = Math.floor(m / 60);
            return "~" + h + "h " + (m % 60) + "m";
        };

        /**
         * Build the API URL that proxies a file's SD-card preview JPEG.
         *
         * @param {string} name - The video file name.
         * @returns {string} A `simpleapi` GET URL with `?thumb=<name>`.
         */
        self._thumbUrl = function (name) {
            return (
                OctoPrint.getSimpleApiUrl("bambucam") +
                "?thumb=" +
                encodeURIComponent(name)
            );
        };

        /**
         * Open the clicked row's preview in the full-size lightbox overlay.
         *
         * @param {Object} row - The clicked timelapse row.
         */
        self.showThumbnail = function (row) {
            self.lightboxUrl(row.thumbUrl);
        };

        /** Close the lightbox overlay. */
        self.hideThumbnail = function () {
            self.lightboxUrl("");
        };

        /**
         * Hide the preview cell when the SD card has no thumbnail (404).
         *
         * @param {Object} row - The row whose image failed to load.
         */
        self.onThumbError = function (row) {
            row.thumbOk(false);
        };

        /**
         * Format a byte count as a human-readable string.
         *
         * @param {number} bytes - Size in bytes (may be null/undefined).
         * @returns {string} e.g. `"12.3 MB"` or `"?"` when unknown.
         */
        self._humanSize = function (bytes) {
            if (bytes === null || bytes === undefined) return "?";
            var units = ["B", "KB", "MB", "GB", "TB"];
            var i = 0;
            var n = bytes;
            while (n >= 1024 && i < units.length - 1) {
                n /= 1024;
                i++;
            }
            return n.toFixed(i === 0 ? 0 : 1) + " " + units[i];
        };

        self._rowByName = function (name) {
            return self.timelapseFiles().find(function (f) {
                return f.name === name;
            });
        };

        /**
         * Fetch the SD-card timelapse listing over the simple API.
         *
         * @memberof BambucamViewModel
         */
        self.refreshTimelapses = function () {
            self.timelapseLoading(true);
            self.timelapseReason("");
            OctoPrint.simpleApiCommand("bambucam", "list_timelapses", {})
                .done(function (response) {
                    if (response.ok) {
                        self.timelapseFiles(
                            (response.files || []).map(self._makeRow),
                        );
                    } else {
                        self.timelapseFiles([]);
                        self.timelapseReason(response.reason || "error");
                    }
                })
                .fail(function () {
                    self.timelapseFiles([]);
                    self.timelapseReason("error");
                })
                .always(function () {
                    self.timelapseLoading(false);
                });
            self.refreshLocalAvi();
        };

        /**
         * Build a Knockout row for a local .avi awaiting conversion.
         *
         * @param {Object} f - `{name, size}` record from `list_local_avi`.
         * @returns {Object} Row observable bundle.
         */
        self._makeLocalRow = function (f) {
            var row = {
                name: f.name,
                size: f.size,
                converting: ko.observable(false),
                convertProgress: ko.observable(0),
                rowError: ko.observable(""),
            };
            row.sizeText = ko.pureComputed(function () {
                return self._humanSize(row.size);
            });
            return row;
        };

        self._localRowByName = function (name) {
            return self.localAvi().find(function (f) {
                return f.name === name;
            });
        };

        /** Fetch the list of local .avi files awaiting conversion. */
        self.refreshLocalAvi = function () {
            OctoPrint.simpleApiCommand("bambucam", "list_local_avi", {}).done(
                function (response) {
                    if (response.ok) {
                        self.localAvi(
                            (response.files || []).map(self._makeLocalRow),
                        );
                    }
                },
            );
        };

        self._startConvert = function (names) {
            if (names.length === 0 || self.convertRunning()) return;
            self.convertRunning(true);
            names.forEach(function (name) {
                var row = self._localRowByName(name);
                if (row) {
                    row.converting(true);
                    row.convertProgress(0);
                    row.rowError("");
                }
            });
            OctoPrint.simpleApiCommand("bambucam", "convert_local_avi", {
                names: names,
            })
                .done(function (response) {
                    if (!response.ok) {
                        self.convertRunning(false);
                        self.localAvi().forEach(function (r) {
                            r.converting(false);
                        });
                        self._opRejected(response.reason);
                    }
                })
                .fail(function () {
                    self.convertRunning(false);
                    self._opRejected("error");
                });
        };

        self.convertOneLocal = function (row) {
            self._startConvert([row.name]);
        };

        self.convertAllLocal = function () {
            self._startConvert(
                self.localAvi().map(function (r) {
                    return r.name;
                }),
            );
        };

        self._startOp = function (op, command) {
            var names = self.selectedNames();
            if (names.length === 0) return;
            self.opRunning(true);
            self.selectedRows().forEach(function (row) {
                row.progress(0);
                row.rowError("");
                row.etaText("");
                row.converting(false);
                row.convertProgress(0);
                row.renamedTo("");
                row._etaStart = 0;
                row._etaStartBytes = 0;
            });
            OctoPrint.simpleApiCommand("bambucam", command, { names: names })
                .done(function (response) {
                    if (!response.ok) {
                        self.opRunning(false);
                        self.selectedRows().forEach(function (row) {
                            row.progress(-1);
                        });
                        self._opRejected(response.reason);
                    }
                })
                .fail(function () {
                    self.opRunning(false);
                    self._opRejected("error");
                });
        };

        self._opRejected = function (reason) {
            var messages = {
                busy: gettext("An operation is already running."),
                printing: gettext("Not available while a print is running."),
                bad_name: gettext("No valid files selected."),
                error: gettext("Request failed."),
            };
            new PNotify({
                title: "BambuCam",
                text: messages[reason] || reason,
                type: "error",
                hide: true,
            });
        };

        // Persistent toast shown while ffmpeg re-encodes, mirroring OctoPrint's
        // own "rendering timelapse" notice (incl. the performance hint). Held as
        // a single instance and removed once no conversion is running anymore.
        self._convertingToast = undefined;

        /** Show the persistent "converting" toast (no-op if already up). */
        self._showConvertingToast = function () {
            if (self._convertingToast) return;
            self._convertingToast = new PNotify({
                title: gettext("Converting timelapse"),
                text: gettext(
                    "Re-encoding the video to .mp4. This can take a while and " +
                        "may slow down the printer/host until it is done.",
                ),
                type: "notice",
                hide: false,
                icon: "fa fa-spinner fa-spin",
            });
        };

        /** Remove the persistent "converting" toast (no-op if not up). */
        self._hideConvertingToast = function () {
            if (!self._convertingToast) return;
            self._convertingToast.remove();
            self._convertingToast = undefined;
        };

        self.copySelected = function () {
            // Files already present in the timelapse folder (server set
            // `copied`) would land as a `-N` numbered duplicate. Warn first and
            // let the user decide whether to copy them again.
            var dupes = self
                .selectedRows()
                .filter(function (row) {
                    return row.copied();
                })
                .map(function (row) {
                    return row.name;
                });
            if (dupes.length === 0) {
                self._startOp("copy", "copy_timelapses");
                return;
            }
            var message =
                gettext(
                    "The following files were already copied and will be saved again as a numbered copy:",
                ) +
                "\n\n" +
                dupes.join("\n") +
                "\n\n" +
                gettext("Copy them again?");
            showConfirmationDialog({
                title: gettext("Copy timelapses"),
                message: message,
                proceed: gettext("Copy again"),
                onproceed: function () {
                    self._startOp("copy", "copy_timelapses");
                },
            });
        };

        self.moveSelected = function () {
            var names = self.selectedNames();
            self._confirmDestructive(
                gettext("Move timelapses"),
                names,
                function () {
                    self._startOp("move", "move_timelapses");
                },
            );
        };

        self.deleteSelected = function () {
            var names = self.selectedNames();
            self._confirmDestructive(
                gettext("Delete timelapses"),
                names,
                function () {
                    self._startOp("delete", "delete_timelapses");
                },
            );
        };

        /**
         * Show a confirmation dialog listing the affected files before a
         * destructive (move/delete) SD-card operation.
         *
         * @param {string} title - Dialog title.
         * @param {Array<string>} names - Affected file names.
         * @param {Function} onConfirm - Callback run on confirm.
         */
        self._confirmDestructive = function (title, names, onConfirm) {
            var message =
                gettext(
                    "This deletes the following files from the printer SD card and cannot be undone:",
                ) +
                "\n\n" +
                names.join("\n");
            showConfirmationDialog({
                title: title,
                message: message,
                proceed: gettext("Proceed"),
                onproceed: onConfirm,
            });
        };

        /**
         * Apply a `timelapse_op` push message: update a row's progress/error,
         * mark copied/remove on done, and show the batch summary on completion.
         *
         * @param {Object} data - The plugin message payload.
         */
        self._handleTimelapseOp = function (data) {
            if (data.state === "batch_done") {
                self.opRunning(false);
                self._hideConvertingToast();
                self.timelapseFiles().forEach(function (f) {
                    f.progress(-1);
                    // the batch is over: clear the selection so the next
                    // operation starts from a clean slate (rows removed by
                    // move/delete drop their checkbox with them anyway)
                    f.selected(false);
                });
                var s = data.summary || {};
                var parts = [];
                if (s.copied) parts.push(s.copied + " " + gettext("copied"));
                if (s.moved) parts.push(s.moved + " " + gettext("moved"));
                if (s.deleted) parts.push(s.deleted + " " + gettext("deleted"));
                if (s.skipped) parts.push(s.skipped + " " + gettext("skipped"));
                new PNotify({
                    title: "BambuCam",
                    text: parts.join(", ") || gettext("Done."),
                    type: s.skipped ? "notice" : "success",
                    hide: true,
                });
                // a copy may have left a .avi behind (transcode off/skipped)
                self.refreshLocalAvi();
                return;
            }

            var row = self._rowByName(data.name);
            if (!row) return;

            if (data.state === "progress") {
                row.progress(data.percent || 0);
                self._updateEta(row, data);
            } else if (data.state === "converting") {
                // download finished, ffmpeg now re-encoding .avi→.mp4: show its
                // own progress bar (percent parsed from ffmpeg by the backend)
                // plus the persistent performance-hint toast
                row.progress(-1);
                row.etaText("");
                row.converting(true);
                row.convertProgress(data.percent != null ? data.percent : 0);
                self._showConvertingToast();
            } else if (data.state === "done") {
                row.progress(-1);
                row.etaText("");
                row.converting(false);
                row.convertProgress(0);
                // a transcode warning rides along on a successful op (the copy
                // itself worked, only the .avi→.mp4 conversion was skipped)
                if (data.reason) {
                    row.rowError(self._transcodeWarning(data.reason));
                } else if (data.renamed && data.renamed !== row.name) {
                    // converted to a new .mp4 name → show it
                    row.renamedTo(data.renamed);
                }
                if (data.op === "copy") {
                    row.copied(true);
                } else {
                    // move/delete removed the SD original
                    self.timelapseFiles.remove(row);
                }
            } else if (data.state === "error" || data.state === "skipped") {
                row.progress(-1);
                row.etaText("");
                row.converting(false);
                row.convertProgress(0);
                row.rowError(self._opRowReason(data.reason));
            }
        };

        self._opRowReason = function (reason) {
            var messages = {
                no_space: gettext("Not enough free disk space"),
                bad_name: gettext("Invalid file name"),
                name_conflict: gettext("Name conflict"),
                network: gettext("Transfer failed"),
                connection_lost: gettext("Connection lost"),
                transcode_failed: gettext("Conversion failed"),
            };
            return messages[reason] || reason || gettext("Failed");
        };

        /**
         * Apply a `convert_op` push message for the local-.avi conversion list.
         *
         * @param {Object} data - The plugin message payload.
         */
        self._handleConvertOp = function (data) {
            if (data.state === "batch_done") {
                self.convertRunning(false);
                self._hideConvertingToast();
                var s = data.summary || {};
                var parts = [];
                if (s.converted)
                    parts.push(s.converted + " " + gettext("converted"));
                if (s.skipped) parts.push(s.skipped + " " + gettext("skipped"));
                new PNotify({
                    title: "BambuCam",
                    text: parts.join(", ") || gettext("Done."),
                    type: s.skipped ? "notice" : "success",
                    hide: true,
                });
                self.refreshLocalAvi();
                return;
            }
            var row = self._localRowByName(data.name);
            if (!row) return;
            if (data.state === "progress") {
                row.converting(true);
                row.convertProgress(data.percent != null ? data.percent : 0);
                self._showConvertingToast();
            } else if (data.state === "done") {
                self.localAvi.remove(row);
            } else if (data.state === "error" || data.state === "skipped") {
                row.converting(false);
                row.convertProgress(0);
                row.rowError(self._opRowReason(data.reason));
            }
        };

        /**
         * Handle an `auto_sync` push message: a print finished and the plugin
         * is automatically pulling the new timelapse(s). Inform the user and
         * refresh the list so the per-file progress rows are visible.
         *
         * @param {Object} data - `{state, count, action}`.
         */
        self._handleAutoSync = function (data) {
            if (data.state !== "started") return;
            var n = data.count || 0;
            var text =
                data.action === "move"
                    ? gettext(
                          "Print finished — automatically moving %d new timelapse(s) from the printer.",
                      )
                    : gettext(
                          "Print finished — automatically copying %d new timelapse(s) from the printer.",
                      );
            new PNotify({
                title: "BambuCam",
                text: text.replace("%d", n),
                type: "info",
                hide: true,
            });
            // refresh so the batch's per-file progress rows show up live
            self.refreshTimelapses();
        };

        self._transcodeWarning = function (reason) {
            var messages = {
                transcode_failed: gettext(
                    "Copied, but .avi→.mp4 conversion failed",
                ),
                no_ffmpeg: gettext(
                    "Copied as .avi (ffmpeg not configured in OctoPrint)",
                ),
                printing: gettext(
                    "Copied as .avi (conversion skipped while printing)",
                ),
            };
            return messages[reason] || reason;
        };

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
            clearTimeout(self._streamRetryTimer);
            self._streamRetryTimer = setTimeout(self._loadStream, 10000);
        };

        /**
         * Ask the server whether LED control is possible (i.e. the printer
         * serial is known) and show/hide the overlay button accordingly.
         *
         * @memberof BambucamViewModel
         */
        self._fetchLedAvailability = function () {
            OctoPrint.simpleApiGet("bambucam")
                .done(function (status) {
                    self.ledAvailable(!!status.led_available);
                    // Daemon running ⇒ the printer is reachable right now.
                    self.ledOnline(!!status.running);
                    // adopt the real light state if the monitor already knows it
                    if (status.led_on === true || status.led_on === false) {
                        self.ledOn(status.led_on);
                    }
                })
                .fail(function () {
                    self.ledAvailable(false);
                    self.ledOnline(false);
                });
        };

        /**
         * Open the standing light-state monitor (a live MQTT connection that
         * reports the real chamber-light state). Started when the webcam tab
         * becomes visible; safe to call repeatedly.
         *
         * @memberof BambucamViewModel
         */
        self._startLedMonitor = function () {
            if (!self.ledAvailable()) return;
            OctoPrint.simpleApiCommand(
                "bambucam",
                "led_monitor_start",
                {},
            ).done(function (r) {
                if (r && (r.led_on === true || r.led_on === false)) {
                    self.ledOn(r.led_on);
                }
            });
        };

        /**
         * Close the standing light-state monitor when leaving the webcam tab.
         *
         * @memberof BambucamViewModel
         */
        self._stopLedMonitor = function () {
            OctoPrint.simpleApiCommand("bambucam", "led_monitor_stop", {});
        };

        /**
         * Toggle the printer light over MQTT. Optimistic: flip the observable
         * up front. Rapid clicks are coalesced — while a command is in flight
         * the desired state is just remembered, and the difference (if any) is
         * sent once the current command settles. This stops fast on/off taps
         * from stacking connect/publish cycles on the printer's broker.
         *
         * @memberof BambucamViewModel
         */
        self.toggleLed = function () {
            var target = !self.ledOn();
            self.ledOn(target); // optimistic UI
            if (self.ledBusy()) {
                // a command is running; record the latest intent and let the
                // in-flight handler flush it when it finishes
                self._ledPending = target;
                return;
            }
            self._sendLed(target);
        };

        /**
         * Send a single set_led command and, once it settles, flush any state
         * the user requested in the meantime (click coalescing).
         *
         * @memberof BambucamViewModel
         * @param {boolean} target - Desired light state.
         */
        self._sendLed = function (target) {
            self.ledBusy(true);
            self._ledPending = null;
            var fail = function () {
                // only revert the UI if no newer intent is queued
                if (self._ledPending === null) self.ledOn(!target);
                new PNotify({
                    title: gettext("BambuCam"),
                    text: gettext("Could not switch the printer light."),
                    type: "error",
                });
            };
            OctoPrint.simpleApiCommand("bambucam", "set_led", { on: target })
                .done(function (response) {
                    if (!response || !response.ok) fail();
                })
                .fail(fail)
                .always(function () {
                    self.ledBusy(false);
                    // a click arrived while we were busy → send the delta
                    if (
                        self._ledPending !== null &&
                        self._ledPending !== target
                    ) {
                        self._sendLed(self._ledPending);
                    } else {
                        self._ledPending = null;
                    }
                });
        };

        /**
         * Run the printer connection test via the simple API and surface the
         * result in the status panel.
         *
         * @memberof BambucamViewModel
         */
        /**
         * Probe whether OctoPrint-BambuConnector is installed and can supply
         * the printer IP / access code, and fill the read-only auto fields.
         *
         * Best-effort: on any failure or when no usable data is found, the
         * "Auto" option stays disabled and the user keeps manual entry.
         */
        self.detectConnector = function () {
            OctoPrint.simpleApiCommand("bambucam", "detect_connector", {})
                .done(function (response) {
                    var c = (response && response.connector) || {};
                    self.connectorAvailable(!!c.available);
                    self.connectorHostname(c.hostname || "");
                    self.connectorAccessCodeMask(
                        c.has_access_code ? "••••••••" : "",
                    );
                    // a stale "auto" selection with no connector → fall back
                    if (
                        !c.available &&
                        self.settings &&
                        self.settings.plugins.bambucam.config_source() ===
                            "auto"
                    ) {
                        self.settings.plugins.bambucam.config_source("manual");
                    }
                })
                .fail(function () {
                    self.connectorAvailable(false);
                });
        };

        /**
         * Probe whether OctoPrint's ffmpeg (used for .avi→.mp4 conversion) is
         * configured and runnable, for the Timelapse-settings indicator.
         */
        self.fetchFfmpegStatus = function () {
            self.ffmpegState("");
            OctoPrint.simpleApiCommand("bambucam", "ffmpeg_status", {})
                .done(function (response) {
                    var f = (response && response.ffmpeg) || {};
                    self.ffmpegState(f.executable ? "ok" : "missing");
                    self.ffmpegPath(f.path || "");
                })
                .fail(function () {
                    self.ffmpegState("missing");
                    self.ffmpegPath("");
                });
        };

        self.testConnection = function () {
            var plugin = self.settings.plugins.bambucam;
            self.testing(true);
            self.testResult("");
            // In auto mode send an empty access code so the server tests with
            // the credentials Bambu Connector supplies (the browser never has
            // the plaintext code in that mode).
            var auto = self.connectionFieldsAuto();
            OctoPrint.simpleApiCommand("bambucam", "test_connection", {
                hostname: auto ? self.connectorHostname() : plugin.hostname(),
                access_code: auto ? "" : plugin.access_code(),
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
            self.detectConnector();
            self.fetchFfmpegStatus();
        };

        self.onSettingsHidden = function () {
            clearInterval(self._statusTimer);
        };

        self.onSettingsSaved = function () {
            // daemon may have been restarted with a new port → reconnect stream
            setTimeout(self._loadStream, 2000);
        };

        self.onBeforeBinding = function () {
            self.settings = self.settingsViewModel.settings;
        };

        self.onStartupComplete = function () {
            self._loadStream();
            self._syncHeaderBackground();
            // fetch availability first, then start the monitor if the Control
            // tab (where the webcam lives) is the one shown on load
            OctoPrint.simpleApiGet("bambucam").done(function (status) {
                self.ledAvailable(!!status.led_available);
                self.ledOnline(!!status.running);
                if (status.led_on === true || status.led_on === false) {
                    self.ledOn(status.led_on);
                }
                var active =
                    window.location.hash ||
                    (OctoPrint.coreui || {}).selectedTab;
                if (active === "#control") self._startLedMonitor();
            });
        };

        /**
         * Lazy-load the timelapse list the first time the tab is shown (FTP is
         * slow and single-connection, so don't fetch eagerly).
         *
         * @param {string} current - The id of the now-active tab.
         */
        self.onTabChange = function (current, previous) {
            if (current === "#tab_plugin_bambucam") {
                self._syncHeaderBackground();
                if (!self._timelapseLoaded) {
                    self._timelapseLoaded = true;
                    self.refreshTimelapses();
                }
            }
            // The webcam stream lives in OctoPrint's Control tab; keep the live
            // light-state monitor open only while that tab is visible.
            if (current === "#control") {
                self._startLedMonitor();
            } else if (previous === "#control") {
                self._stopLedMonitor();
            }
        };

        /**
         * Match the sticky table header to the active theme.
         *
         * Themes (incl. Themeify dark modes) color the page background at
         * different ancestors and leave the elements above our header
         * transparent, so neither a fixed color nor CSS `inherit` is reliable.
         * Walk up from the tab and copy the first opaque (non-transparent)
         * computed background-color into the `--bambucam-header-bg` custom
         * property, which the sticky `<th>` consumes (see BambuCam.css).
         *
         * @memberof BambucamViewModel
         */
        self._syncHeaderBackground = function () {
            var tab = document.getElementById("tab_plugin_bambucam");
            if (!tab) return;
            var el = tab;
            var bg = "";
            while (el) {
                var c = window.getComputedStyle(el).backgroundColor;
                if (c && c !== "transparent" && !/rgba\(0, 0, 0, 0\)/.test(c)) {
                    bg = c;
                    break;
                }
                el = el.parentElement;
            }
            if (bg) {
                tab.style.setProperty("--bambucam-header-bg", bg);
            }
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
            if (data.type === "timelapse_op") {
                self._handleTimelapseOp(data);
                return;
            }
            if (data.type === "convert_op") {
                self._handleConvertOp(data);
                return;
            }
            if (data.type === "auto_sync") {
                self._handleAutoSync(data);
                return;
            }
            if (data.type === "led_state") {
                // real chamber-light state pushed by the monitor — adopt it
                // unless a user-initiated command is still settling
                if (!self.ledBusy() && self._ledPending === null) {
                    self.ledOn(!!data.on);
                }
                return;
            }
            if (data.type !== "daemon_state") return;

            if (data.state === "gave_up") {
                self.ledOnline(false); // printer unreachable → hide light
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
                self.ledOnline(false); // hide the light toggle while offline
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
                self.ledOnline(true); // printer reachable again
                self.lastError("");
                setTimeout(self._loadStream, 2000);
            } else if (data.state === "crashed" || data.state === "stopped") {
                self.ledOnline(false);
            }
            self._fetchStatus();
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: BambucamViewModel,
        dependencies: [
            "settingsViewModel",
            "loginStateViewModel",
            "printerStateViewModel",
        ],
        elements: [
            "#settings_plugin_bambucam",
            "#bambucam_webcam_container",
            "#tab_plugin_bambucam",
        ],
    });
});
