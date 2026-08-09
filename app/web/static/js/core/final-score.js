"use strict";

(() => {
  async function submitOrRecover(component, gameKey, recoveredState = null) {
    const player = component.player;
    const session = component.session;
    if (!player || !session || !component.outcome) return null;

    const recoveredScore = recoveredState?.session?.final_score || null;
    if (recoveredScore) {
      component.finalScore = recoveredScore;
      return recoveredScore;
    }

    component.saveJournal({ pendingAction: "submit_score" });
    try {
      const score = await window.ArcadeProof.api.submitScore(player.id, session.id);
      component.finalScore = score;
      component.saveJournal({ pendingAction: component.canClaim ? "claim" : null });
      return score;
    } catch (error) {
      if (!error.uncertain && error.code !== "invalid_transition") throw error;
      const state = await window.ArcadeProof.api.getGameState(player.id, gameKey);
      const score = state.session?.final_score || null;
      if (!score) throw error;
      component.finalScore = score;
      component.saveJournal({ pendingAction: component.canClaim ? "claim" : null });
      return score;
    }
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.finalScores = { submitOrRecover };
})();
