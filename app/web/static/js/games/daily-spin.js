"use strict";

(() => {
  const gameKey = "daily_spin";
  const palette = ["#67f1cf", "#ffbf5a", "#a987ff", "#ff7f73", "#63b3ff", "#f887d1"];
  const svgNamespace = "http://www.w3.org/2000/svg";

  function clientSeed() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  function point(angle, radius = 108) {
    const radians = (angle * Math.PI) / 180;
    return [120 + radius * Math.cos(radians), 120 + radius * Math.sin(radians)];
  }

  function arcPath(start, end) {
    const [startX, startY] = point(start);
    const [endX, endY] = point(end);
    const largeArc = end - start > 180 ? 1 : 0;
    return `M 120 120 L ${startX} ${startY} A 108 108 0 ${largeArc} 1 ${endX} ${endY} Z`;
  }

  function renderWheelSegments(segments) {
    const container = document.getElementById("daily-spin-segments");
    if (!container) return;
    const groups = segments.map((segment) => {
      const group = document.createElementNS(svgNamespace, "g");
      const path = document.createElementNS(svgNamespace, "path");
      const label = document.createElementNS(svgNamespace, "text");
      group.setAttribute("data-segment-key", segment.key);
      path.setAttribute("d", segment.path);
      path.setAttribute("fill", segment.fill);
      label.setAttribute("x", segment.labelX);
      label.setAttribute("y", segment.labelY);
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("dominant-baseline", "middle");
      label.textContent = String(segment.value);
      group.append(path, label);
      return group;
    });
    container.replaceChildren(...groups);
  }

  function dailySpin() {
    return {
      busy: false,
      status: "loading",
      message: "Getting the wheel ready…",
      config: null,
      session: null,
      outcome: null,
      reward: null,
      finalScore: null,
      proof: null,
      verification: null,
      segments: [],
      wheelRotation: 0,
      wheelAnimation: null,
      resultLabel: "",
      countdownLabel: "",
      timer: null,
      get player() { return window.ArcadeProof.playerStore.current(); },
      get canPlay() { return !this.busy && ["ready", "result"].includes(this.status); },
      get canClaim() { return Boolean(this.reward && this.reward.status === "pending"); },
      get isResult() { return Boolean(this.outcome); },
      get isVerified() { return Boolean(this.verification?.verified); },
      get hasProof() { return Boolean(this.proof); },
      get countdownPrefix() {
        return this.session?.status === "active" ? "Active spin expires in" : "Next spin in";
      },
      get actionLabel() {
        if (this.session?.status === "active") return "Resume spin";
        return "Spin now";
      },
      get rewardLabel() {
        if (!this.reward) return "Points are being prepared";
        return this.reward.status === "pending"
          ? `${this.reward.value} points ready to collect`
          : `${this.reward.value} points collected`;
      },
      get proofText() { return this.proof ? JSON.stringify(this.proof, null, 2) : ""; },
      async initialize() {
        window.addEventListener("arcade-proof:player", () => this.load());
        await this.load();
      },
      destroy() {
        this.wheelAnimation?.cancel();
        window.clearInterval(this.timer);
      },
      resetWheel() {
        this.wheelAnimation?.cancel();
        this.wheelAnimation = null;
        this.wheelRotation = 0;
      },
      async load() {
        this.busy = true;
        this.message = "Getting the wheel ready…";
        try {
          this.config = await window.ArcadeProof.api.getGameConfig(gameKey);
          this.buildSegments();
          await this.recover();
        } catch (error) {
          this.status = "terminal_error";
          this.message = error.message;
        } finally {
          this.busy = false;
        }
      },
      buildSegments() {
        const rewards = this.config?.payload?.rewards || [];
        const total = rewards.reduce((sum, band) => sum + band.weight, 0);
        let cursor = -90;
        this.segments = rewards.map((band, index) => {
          const sweep = (band.weight / total) * 360;
          const center = cursor + sweep / 2;
          const [labelX, labelY] = point(center, 68);
          const segment = {
            ...band,
            path: arcPath(cursor, cursor + sweep),
            fill: palette[index % palette.length],
            labelX,
            labelY,
            percentage: `${Math.round((band.weight / total) * 100)}%`,
            center,
          };
          cursor += sweep;
          return segment;
        });
        renderWheelSegments(this.segments);
      },
      saveJournal(values) {
        if (!this.player) return;
        const current = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
        window.ArcadeProof.journal.save(this.player.id, gameKey, { ...current, ...values });
      },
      async recover() {
        window.clearInterval(this.timer);
        this.countdownLabel = "";
        if (!this.player) {
          this.status = "ready";
          this.message = "Choose a player to spin the wheel.";
          return;
        }
        this.status = "recovering";
        const state = await window.ArcadeProof.api.getGameState(this.player.id, gameKey);
        this.session = state.session;
        this.outcome = state.session?.outcome || null;
        this.reward = state.session?.reward || null;
        this.finalScore = state.session?.final_score || null;
        const countdownTarget = this.session?.status === "active"
          ? this.session.expires_at
          : state.next_play_at;
        if (countdownTarget) this.startCountdown(state.server_time, countdownTarget);
        if (!this.session) {
          this.status = state.next_play_at ? "cooldown" : "ready";
          this.message = "The wheel is ready. Take your spin.";
          return;
        }
        this.saveJournal({
          requestId: this.session.request_id,
          sessionId: this.session.id,
          commitmentId: this.session.fairness?.proof_id || null,
          outcome: this.outcome,
        });
        if (this.session.status === "active") {
          this.status = "ready";
          this.message = this.session.fairness
            ? "Your spin is saved and ready to resume."
            : "Your unfinished spin is ready to resume.";
          return;
        }
        if (this.session.status === "expired") {
          this.status = "expired";
          this.message = "That spin expired before it finished. Try again when the timer ends.";
          return;
        }
        if (this.outcome) {
          this.presentResult(false);
          if (!this.finalScore) {
            await window.ArcadeProof.finalScores.submitOrRecover(this, gameKey, state);
          }
          await this.loadProof();
          this.status = state.next_play_at ? "cooldown" : "result";
          this.message = "Your finished spin is back on screen.";
          return;
        }
        this.status = state.next_play_at ? "cooldown" : "ready";
      },
      startCountdown(serverTime, target) {
        const clock = new window.ArcadeProof.ServerClock(serverTime);
        const render = () => {
          this.countdownLabel = clock.label(target);
          if (clock.remaining(target) === 0) {
            window.clearInterval(this.timer);
            window.setTimeout(() => this.recover(), 0);
          }
        };
        render();
        this.timer = window.setInterval(render, 1000);
      },
      async action() {
        if (!this.player) {
          document.getElementById("identity-dialog")?.showModal();
          return;
        }
        this.busy = true;
        this.status = "submitting";
        this.message = "Starting your spin…";
        try {
          let journal = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
          if (!this.session || this.session.status !== "active") {
            this.outcome = null;
            this.reward = null;
            this.finalScore = null;
            this.proof = null;
            this.verification = null;
            this.resultLabel = "";
            this.resetWheel();
            const requestId = crypto.randomUUID();
            this.saveJournal({ requestId, pendingAction: "create_session" });
            this.session = await window.ArcadeProof.api.createSession(this.player.id, requestId, gameKey);
            this.saveJournal({ sessionId: this.session.id, pendingAction: "commit" });
            journal = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
          }
          let proofId = this.session.fairness?.proof_id || journal.commitmentId;
          if (!proofId) {
            const commitment = await window.ArcadeProof.api.commitFairness(this.player.id, this.session.id);
            proofId = commitment.proof_id;
            this.saveJournal({ commitmentId: proofId, pendingAction: "evaluate" });
          }
          journal = window.ArcadeProof.journal.get(this.player.id, gameKey) || {};
          const seed = journal.clientSeed || clientSeed();
          this.saveJournal({ clientSeed: seed, pendingAction: "evaluate" });
          this.message = "The wheel is finding your prize…";
          const evaluated = await window.ArcadeProof.api.evaluateFairness(this.player.id, proofId, seed);
          this.outcome = evaluated.outcome;
          this.reward = evaluated.reward;
          this.saveJournal({ outcome: this.outcome, pendingAction: "submit_score" });
          await this.animateResult();
          await window.ArcadeProof.finalScores.submitOrRecover(this, gameKey);
          await this.loadProof();
          this.status = "result";
          this.message = this.isVerified
            ? "Your result is ready and its fairness check passed."
            : "Your result is ready. Fairness details need another check.";
        } catch (error) {
          this.status = error.code === "session_expired" ? "expired" : "recovering";
          this.message = error.message;
          if (error.uncertain || error.code === "active_session_exists") await this.recover();
        } finally {
          this.busy = false;
        }
      },
      presentResult(animate = true) {
        const rewardKey = this.outcome?.result?.reward_key;
        const segment = this.segments.find((candidate) => candidate.key === rewardKey);
        this.resultLabel = segment
          ? `${segment.value} points from ${segment.key.replaceAll("_", " ")}`
          : "Your result is ready";
        if (!segment) return 0;
        const reduced = window.ArcadeProof.preferences.reducedMotion();
        const turns = animate && !reduced ? 5 : 0;
        const rotation = turns * 360 + (-90 - segment.center);
        const duration = animate && !reduced ? 2200 : 0;
        const wheel = document.getElementById("daily-spin-wheel");
        if (!wheel) return 0;
        this.wheelAnimation?.cancel();
        this.wheelAnimation = wheel.animate(
          [
            { transform: `rotate(${this.wheelRotation}deg)` },
            { transform: `rotate(${rotation}deg)` },
          ],
          {
            duration,
            easing: "cubic-bezier(0.12, 0.72, 0.15, 1)",
            fill: "forwards",
          },
        );
        this.wheelRotation = rotation;
        return duration;
      },
      async animateResult() {
        this.status = "animating";
        const duration = this.presentResult(true);
        if (duration) await new Promise((resolve) => window.setTimeout(resolve, duration));
        window.setTimeout(() => document.getElementById("spin-result-title")?.focus(), 0);
      },
      async loadProof() {
        if (!this.player || !this.outcome) return;
        this.proof = await window.ArcadeProof.api.getFairnessProof(this.player.id, this.outcome.id);
        this.verification = await window.ArcadeProof.api.verifyFairness(this.player.id, this.outcome.id);
        this.saveJournal({ pendingAction: this.canClaim ? "claim" : null });
      },
      async claim() {
        if (!this.player || !this.session || !this.canClaim) return;
        this.busy = true;
        this.saveJournal({ pendingAction: "claim" });
        try {
          this.reward = await window.ArcadeProof.api.claimSession(this.player.id, this.session.id);
          window.ArcadeProof.journal.discard(this.player.id, gameKey);
          window.ArcadeProof.notify(`${this.reward.value} points collected.`, "success");
        } catch (error) {
          this.message = error.message;
        } finally {
          this.busy = false;
        }
      },
    };
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.games = window.ArcadeProof.games || {};
  window.ArcadeProof.games.dailySpin = dailySpin;
})();
