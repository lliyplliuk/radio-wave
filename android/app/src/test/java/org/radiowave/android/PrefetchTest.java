package org.radiowave.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.Set;
import java.util.concurrent.FutureTask;
import org.junit.Test;

public class PrefetchTest {
    @Test public void advancingKeepsTheRequestedTransferUntilItLeavesTheWindow() {
        var prefetch = new PlaybackService.PrefetchWindow();
        var next = new PlaybackService.Prefetch();
        var obsolete = new PlaybackService.Prefetch();
        next.work = new FutureTask<>(() -> null);
        obsolete.work = new FutureTask<>(() -> null);
        prefetch.jobs.put("next", next);
        prefetch.jobs.put("obsolete", obsolete);

        prefetch.retainJobs(Set.of("later"), "next");

        assertFalse(next.work.isCancelled());
        assertTrue(obsolete.work.isCancelled());
        assertEquals(Set.of("next"), prefetch.jobs.keySet());

        prefetch.retainJobs(Set.of("later"), null);

        assertTrue(next.work.isCancelled());
        assertTrue(prefetch.jobs.isEmpty());
    }

    @Test public void onlyUpcomingFailuresWakeTheSchedulerAfterTheirCooldown() {
        var prefetch = new PlaybackService.PrefetchWindow();
        prefetch.retryAt.put("next", 200L);
        prefetch.retryAt.put("later", 250L);
        prefetch.retryAt.put("offscreen", 150L);

        assertEquals(200L, prefetch.nextRetry(Set.of("next", "later"), 100L));
        assertEquals(250L, prefetch.nextRetry(Set.of("next", "later"), 200L));
        assertFalse(prefetch.retryAt.containsKey("next"));
        assertEquals(Long.MAX_VALUE, prefetch.nextRetry(Set.of("next", "later"), 250L));
    }
}
