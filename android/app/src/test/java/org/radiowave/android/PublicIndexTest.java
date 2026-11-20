package org.radiowave.android;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.List;

import org.junit.Test;

public final class PublicIndexTest {
    private static final String HASH = "0000000000000000000000000000000000000000";
    private static final String MAGNET = "magnet:?xt=urn:btih:" + HASH;

    @Test
    public void ruTorParsesRealRowShapeAndRejectsUnknownHosts() throws IOException {
        String valid = "<tr><td><a class='downgif' href='https://d.rutor.info/download/42'>↓</a>"
                + "<a href='" + MAGNET + "&amp;dn=Nagart'>magnet</a>"
                + "<a href='/torrent/42/nagart'><b>Nagart</b> — Пугало [FLAC]</a></td>"
                + "<td><span class='green'>21</span></td></tr>";
        String malicious = "<tr><td><a class='downgif' href='https://untrusted.invalid/download/4'>↓</a>"
                + "<a href='/torrent/4/foreign'>Ненужное</a></td></tr>";
        var releases = PublicIndex.parseRuTor("<div id='index'><table>" + valid + malicious + "</table></div>",
                URI.create("https://rutor.info"));
        assertEquals(1, releases.size());
        assertEquals("Nagart — Пугало [FLAC]", releases.get(0).title());
        assertEquals(21, releases.get(0).seeders());
        assertEquals("https://d.rutor.info/download/42", releases.get(0).torrent().toString());
    }

    @Test
    public void jacRedRejectsVideoAndXmlEntityDeclarations() throws IOException {
        String xml = "<rss xmlns:t='http://torznab.com/schemas/2015/feed'><channel>"
                + "<item><title>Музыка [FLAC]</title><t:attr name='magneturl' value='" + MAGNET
                + "'/><t:attr name='seeders' value='44'/></item>"
                + "<item><title>Film 1080p FLAC</title><t:attr name='magneturl' value='" + MAGNET
                + "'/><t:attr name='seeders' value='88'/></item>"
                + "<item><title>Music [MP3]</title><category>2000</category>"
                + "<enclosure url='" + MAGNET + "'/></item></channel></rss>";
        var releases = PublicIndex.parseJacRed(xml.getBytes(StandardCharsets.UTF_8));
        assertEquals(1, releases.size());
        assertEquals(44, releases.get(0).seeders());
        assertThrows(IOException.class, () -> PublicIndex.parseJacRed(
                ("<!DOCTYPE data [<!ENTITY external SYSTEM 'file:///etc/passwd'>]>" + xml)
                        .getBytes(StandardCharsets.UTF_8)));
    }

    @Test
    public void hashesAreCanonicalAndAmbiguityIsRejected() {
        assertEquals(HASH, PublicIndex.infoHash("magnet:?xt=urn%3Abtih%3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"));
        assertEquals(HASH, PublicIndex.infoHash(MAGNET + "&xt=urn:btih:" + HASH));
        assertEquals("", PublicIndex.infoHash(MAGNET + "&xt=urn:btih:1111111111111111111111111111111111111111"));
        assertEquals("", PublicIndex.infoHash("https://fake/?xt=urn:btih:" + HASH));
        assertThrows(IOException.class, () -> PublicIndex.bounded(new ByteArrayInputStream(new byte[4]), 3, Long.MAX_VALUE));
    }

    @Test
    public void recordingMatchesActualArtistAndTrackNameWithIndependentReleaseName() {
        var named = TorrentSources.describe("Nagart — Сборник [FLAC]", "Disk1/07. Мёртвая Алёна.flac");
        assertEquals("Nagart", named.artist());
        assertEquals("Мёртвая Алёна", named.title());
        assertTrue(named.matches(List.of("nagart", "алена")));
        assertFalse(named.matches(List.of("nagart", "пугало")));
        assertFalse(named.live());
        assertTrue(TorrentSources.describe("Автор — Live 2020", "Песня.mp3").live());
        assertEquals("Ю. Лоза", TorrentSources.describe("Русский хит", "003. Ю. Лоза - Плот.mp3").artist());
    }
}
