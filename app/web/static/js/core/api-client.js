"use strict";

(() => {
  const messages = Object.freeze({
    active_session_exists: "An active play already exists. Refreshing its server state is safest.",
    cooldown_active: "This game is still cooling down. Refresh to get the authoritative ready time.",
    forbidden: "This play belongs to a different local demo player.",
    idempotency_conflict: "This saved operation no longer matches the original request.",
    invalid_transition: "That play has already moved to another server state. Recovering its record is safest.",
    leaderboard_entry_not_found: "You do not have a score in this weekly board yet.",
    not_found: "That player or play could not be found.",
    player_inactive: "This demo player is inactive.",
    session_expired: "This play expired before completion.",
    invalid_play: "The server could not accept that action.",
  });

  class ApiError extends Error {
    constructor(code, message, options = {}) {
      super(message);
      this.name = "ApiError";
      this.code = code;
      this.status = options.status || 0;
      this.uncertain = Boolean(options.uncertain);
      this.retryable = Boolean(options.retryable);
    }
  }

  class ApiClient {
    constructor(options = {}) {
      this.baseUrl = options.baseUrl || "/api/v1";
      this.timeoutMs = options.timeoutMs || 10000;
    }

    async request(path, options = {}) {
      const method = options.method || "GET";
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), options.timeoutMs || this.timeoutMs);
      const headers = { Accept: "application/json" };
      if (options.playerId) headers["X-Player-ID"] = options.playerId;
      if (options.body !== undefined) headers["Content-Type"] = "application/json";

      try {
        const response = await fetch(`${this.baseUrl}${path}`, {
          method,
          headers,
          body: options.body === undefined ? undefined : JSON.stringify(options.body),
          signal: controller.signal,
          credentials: "same-origin",
        });
        const contentType = response.headers.get("content-type") || "";
        const payload = contentType.includes("application/json") ? await response.json() : null;
        if (!response.ok) {
          const code = payload?.error?.code || `http_${response.status}`;
          throw new ApiError(code, messages[code] || "The server could not complete that request.", {
            status: response.status,
            retryable: response.status >= 500,
          });
        }
        return payload;
      } catch (error) {
        if (error instanceof ApiError) throw error;
        const timedOut = error instanceof DOMException && error.name === "AbortError";
        const message = timedOut
          ? "The request timed out. We do not know whether it completed."
          : "The network request failed. Reconnect to recover this play.";
        throw new ApiError(timedOut ? "timeout" : "network_error", message, {
          uncertain: method !== "GET",
          retryable: true,
        });
      } finally {
        window.clearTimeout(timeout);
      }
    }

    createPlayer(displayName) {
      return this.request("/players", { method: "POST", body: { display_name: displayName } });
    }

    getPlayer(playerId) {
      return this.request(`/players/${encodeURIComponent(playerId)}`, { playerId });
    }

    getGameState(playerId, gameKey) {
      const query = new URLSearchParams({ game_key: gameKey });
      return this.request(`/players/${encodeURIComponent(playerId)}/game-state?${query}`, { playerId });
    }

    getGameConfig(gameKey) {
      return this.request(`/games/${encodeURIComponent(gameKey)}/config`);
    }

    createSession(playerId, requestId, gameKey) {
      return this.request("/sessions", {
        method: "POST",
        playerId,
        body: { request_id: requestId, player_id: playerId, game_key: gameKey },
      });
    }

    playSession(playerId, sessionId, intent) {
      return this.request(`/sessions/${encodeURIComponent(sessionId)}/play`, {
        method: "POST",
        playerId,
        body: intent,
      });
    }

    claimSession(playerId, sessionId) {
      return this.request(`/sessions/${encodeURIComponent(sessionId)}/claim`, {
        method: "POST",
        playerId,
        body: {},
      });
    }

    commitFairness(playerId, sessionId) {
      return this.request("/fairness/commit", {
        method: "POST",
        playerId,
        body: { session_id: sessionId },
      });
    }

    evaluateFairness(playerId, proofId, clientSeed) {
      return this.request("/fairness/evaluate", {
        method: "POST",
        playerId,
        body: { proof_id: proofId, client_seed: clientSeed },
      });
    }

    getFairnessProof(playerId, outcomeId) {
      return this.request(`/fairness/outcomes/${encodeURIComponent(outcomeId)}/proof`, { playerId });
    }

    verifyFairness(playerId, outcomeId) {
      return this.request(`/fairness/outcomes/${encodeURIComponent(outcomeId)}/verify`, { playerId });
    }

    submitScore(playerId, sessionId) {
      return this.request("/scores", {
        method: "POST",
        playerId,
        body: { session_id: sessionId },
      });
    }

    getLeaderboard(playerId, gameKey, periodStart, cursor = 0, limit = 10) {
      const query = new URLSearchParams({
        period_start: periodStart,
        cursor: String(cursor),
        limit: String(limit),
      });
      return this.request(`/leaderboards/${encodeURIComponent(gameKey)}?${query}`, { playerId });
    }

    getPlayerRank(playerId, gameKey, periodStart) {
      const query = new URLSearchParams({ game_key: gameKey, period_start: periodStart });
      return this.request(`/players/${encodeURIComponent(playerId)}/rank?${query}`, { playerId });
    }

    cancelSession(playerId, sessionId) {
      return this.request(`/sessions/${encodeURIComponent(sessionId)}`, {
        method: "DELETE",
        playerId,
      });
    }
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.ApiError = ApiError;
  window.ArcadeProof.api = new ApiClient();
})();
