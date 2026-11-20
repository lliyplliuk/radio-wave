package org.radiowave.android;

import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.xml.sax.InputSource;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.StringReader;
import java.io.UnsupportedEncodingException;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URI;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.text.Normalizer;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import javax.xml.parsers.DocumentBuilderFactory;

final class PublicIndex {
    record Release(String title, String magnet, URI torrent, int seeders, String reference) {
        String key() {
            String hash = infoHash(magnet);
            return hash.isEmpty() ? reference : hash;
        }
    }

    private static final Set<String> RUTOR = new LinkedHashSet<>(Arrays.asList("rutor.info", "rutor.is"));
    private static final Set<String> DOWNLOADS = new LinkedHashSet<>(Arrays.asList("rutor.info", "rutor.is", "d.rutor.info", "d.rutor.is"));
    private static final Set<String> JACRED = Collections.singleton("jac.black");
    private static final String TORZNAB_NS = "http://torznab.com/schemas/2015/feed";
    private static final Pattern DOWNLOAD_PATH = Pattern.compile("/download/[0-9]+/?");
    private static final Pattern DETAILS_PATH = Pattern.compile("/torrent/[0-9]+(?:/.*)?");
    private static final Pattern API_PATH = Pattern.compile("/torznab/api/?");
    private static final Pattern INFO_HASH = Pattern.compile("urn:btih:([0-9a-f]{40}|[a-z2-7]{32})", Pattern.CASE_INSENSITIVE);
    private static final Pattern AUDIO = Pattern.compile("(?iu)\\b(?:mp3|мр3|flac|alac|ape|wav|ogg|opus|m4a|lossless)\\b");
    private static final Pattern VIDEO = Pattern.compile("(?iu)\\b(?:x26[45]|h[. -]?26[45]|hdtv|mkv|[bd]drip|hevc|av1|(?:480|576|720|1080|2160)[pi]|[48]k)\\b");
    private static final Pattern CATEGORY = Pattern.compile("(?<![0-9])(?:3020|3030|2[0-9]{3})(?![0-9])");
    private static final Pattern INDEX = Pattern.compile("(?i)<div\\b[^>]*\\bid\\s*=\\s*[\"']?index(?:[\"'\\s>])");
    private static final Pattern ROW = Pattern.compile("(?is)<tr\\b[^>]*>(.*?)</tr\\s*>");
    private static final Pattern LINK = Pattern.compile("(?is)<a\\b([^>]*)>(.*?)</a\\s*>");
    private static final Pattern SEEDERS = Pattern.compile("(?is)<span[^>]*class=[\"'][^\"']*\\bgreen\\b[^\"']*[\"'][^>]*>(.*?)</span\\s*>");
    private static final Pattern ATTR = Pattern.compile("(?i)\\b([a-z]+)\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)'|([^\\s>]+))");
    private static final Pattern ENTITY = Pattern.compile("&(#x[0-9a-fA-F]+|#[0-9]+|amp|quot|apos|nbsp|lt|gt);");
    private static final Pattern WORD = Pattern.compile("[\\p{L}\\p{N}]+");

    private static final int INDEX_LIMIT = 2 * 1024 * 1024;
    static final int TORRENT_LIMIT = 16 * 1024 * 1024;
    private static final int HTTP_TIMEOUT_MS = 3500;

    List<Release> searchRuTor(String query) throws IOException {
        String path = "/search/0/2/100/2/" + encode(String.join(" ", words(query))) + "/";
        return ruTor(path);
    }

    List<Release> searchJacRed(String query) throws IOException {
        URI uri = URI.create("https://jac.black/torznab/api?t=music&cat=3000&q=" + encode(query));
        try {
            return parseJacRed(read(uri, JACRED, API_PATH, INDEX_LIMIT));
        } catch (HttpFailure error) {
            if (error.code != 503) throw error;
            return parseJacRed(read(uri, JACRED, API_PATH, INDEX_LIMIT));
        }
    }

    List<Release> latest() throws IOException {
        return ruTor("/browse/0/2/0/2");
    }

    private List<Release> ruTor(String path) throws IOException {
        IOException failure = null;
        for (String host : RUTOR) {
            if (Thread.currentThread().isInterrupted()) throw new IOException("Поиск отменён");
            URI origin = URI.create("https://" + host);
            try {
                byte[] result = read(origin.resolve(path), RUTOR, null, INDEX_LIMIT);
                return parseRuTor(new String(result, StandardCharsets.UTF_8), origin);
            } catch (IOException error) {
                if (failure == null) failure = error;
            }
        }
        throw new IOException("RuTor не ответил", failure);
    }

    byte[] fetch(Release release) throws IOException {
        byte[] bytes = read(release.torrent(), DOWNLOADS, DOWNLOAD_PATH, TORRENT_LIMIT);
        if (bytes.length < 2 || bytes[0] != 'd' || bytes[bytes.length - 1] != 'e') {
            throw new IOException("Источник вернул файл другого формата");
        }
        return bytes;
    }

    private static String encode(String value) {
        try {
            return URLEncoder.encode(value, "UTF-8").replace("+", "%20");
        } catch (UnsupportedEncodingException impossible) {
            throw new AssertionError(impossible);
        }
    }

    private static boolean allowed(URI uri, Set<String> hosts, Pattern path) {
        return uri != null && "https".equalsIgnoreCase(uri.getScheme()) && uri.getHost() != null
                && hosts.contains(uri.getHost().toLowerCase(Locale.ROOT))
                && uri.getUserInfo() == null && (uri.getPort() == -1 || uri.getPort() == 443)
                && (path == null || path.matcher(uri.getPath()).matches());
    }

    private static byte[] read(URI uri, Set<String> hosts, Pattern allowedPath, int sizeLimit) throws IOException {
        long deadline = System.nanoTime() + HTTP_TIMEOUT_MS * 1_000_000L;
        String sourceHost = uri == null ? "" : uri.getHost();
        for (int redirect = 0; redirect < 3; redirect++) {
            if (!allowed(uri, hosts, allowedPath)) throw new IOException("Недопустимый адрес источника");
            int remaining = (int) ((deadline - System.nanoTime()) / 1_000_000);
            if (remaining <= 0) throw new IOException("Источник долго не отвечает");
            HttpURLConnection connection = (HttpURLConnection) uri.toURL().openConnection(Proxy.NO_PROXY);
            try {
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout(remaining);
                connection.setReadTimeout(remaining);
                connection.setRequestProperty("User-Agent", "RadioWaveAndroid/0.1");
                connection.setRequestProperty("Accept-Encoding", "identity");
                int code = connection.getResponseCode();
                if (code >= 300 && code < 400) {
                    String location = connection.getHeaderField("Location");
                    if (location == null) throw new IOException("Пустое перенаправление источника");
                    uri = uri.resolve(location);
                    continue;
                }
                if (code != 200) throw new HttpFailure(code, sourceHost);
                if (connection.getContentLengthLong() > sizeLimit) throw new IOException("Ответ источника слишком большой");
                try (InputStream input = connection.getInputStream()) {
                    return bounded(input, sizeLimit, deadline);
                }
            } catch (IllegalArgumentException error) {
                throw new IOException("Некорректный ответ источника", error);
            } finally {
                connection.disconnect();
            }
        }
        throw new IOException("Слишком много перенаправлений от источника");
    }

    static byte[] bounded(InputStream input, int limit, long deadline) throws IOException {
        var output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int count;
        while ((count = input.read(buffer)) != -1) {
            if (Thread.currentThread().isInterrupted() || System.nanoTime() - deadline >= 0) {
                throw new IOException("Загрузка метаданных отменена или истекло время");
            }
            if (count > limit - output.size()) throw new IOException("Файл метаданных слишком большой");
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    static List<Release> parseRuTor(String html, URI origin) throws IOException {
        Matcher container = INDEX.matcher(html);
        if (!container.find()) throw new IOException("RuTor не вернул список раздач");
        List<Release> result = new ArrayList<>();
        Matcher rows = ROW.matcher(html).region(container.end(), html.length());
        while (rows.find()) {
            String row = rows.group(1);
            String title = "";
            String magnet = "";
            URI torrent = null;
            URI details = null;
            Matcher links = LINK.matcher(row);
            while (links.find()) {
                String href = "";
                String classes = "";
                Matcher attributes = ATTR.matcher(links.group(1));
                while (attributes.find()) {
                    String value = firstNonNull(attributes.group(2), attributes.group(3), attributes.group(4));
                    if ("href".equalsIgnoreCase(attributes.group(1))) href = unescape(value);
                    if ("class".equalsIgnoreCase(attributes.group(1))) classes = value;
                }
                if (!infoHash(href).isEmpty()) {
                    magnet = href;
                    continue;
                }
                try {
                    URI reference = origin.resolve(href);
                    if (allowed(reference, DOWNLOADS, DOWNLOAD_PATH) && words(classes).contains("downgif")) {
                        torrent = reference;
                    } else if (allowed(reference, RUTOR, DETAILS_PATH)) {
                        title = htmlText(links.group(2));
                        details = reference;
                    }
                } catch (IllegalArgumentException ignored) {
                    // Ignore malformed links from the page.
                }
            }
            if (title.trim().isEmpty() || details == null || (torrent == null && magnet.isEmpty())) continue;
            Matcher seeds = SEEDERS.matcher(row);
            result.add(new Release(title, magnet, torrent, seeds.find() ? count(htmlText(seeds.group(1))) : 0, details.toString()));
        }
        return ordered(result);
    }

    static List<Release> parseJacRed(byte[] bytes) throws IOException {
        String xml = new String(bytes, StandardCharsets.UTF_8);
        if (xml.contains("<!DOCTYPE") || xml.contains("<!ENTITY")) throw new IOException("Недопустимая разметка JacRed");
        try {
            DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            var builder = factory.newDocumentBuilder();
            builder.setEntityResolver((publicId, systemId) -> {
                throw new org.xml.sax.SAXException("Внешняя сущность запрещена");
            });
            Element document = builder.parse(new InputSource(new StringReader(xml))).getDocumentElement();
            if (!"rss".equals(document.getTagName()) || document.getElementsByTagName("channel").getLength() != 1) {
                throw new IOException("JacRed не вернул список раздач");
            }
            var items = document.getElementsByTagName("item");
            List<Release> result = new ArrayList<>();
            for (int index = 0; index < items.getLength(); index++) {
                var item = (Element) items.item(index);
                String title = childValue(item, "title").trim().replaceAll("\\s+", " ");
                if (!AUDIO.matcher(title).find() || VIDEO.matcher(title).find()) continue;
                String magnet = "";
                boolean excluded = false;
                int seeds = 0;
                for (Node node = item.getFirstChild(); node != null; node = node.getNextSibling()) {
                    if (!(node instanceof Element element)) continue;
                    String tag = element.getTagName();
                    if (tag.equals("category") && CATEGORY.matcher(element.getTextContent()).find()) excluded = true;
                    if (tag.equals("enclosure") && magnet.isEmpty()) magnet = element.getAttribute("url");
                    if (!TORZNAB_NS.equals(element.getNamespaceURI()) || !"attr".equals(element.getLocalName())) continue;
                    String value = element.getAttribute("value");
                    switch (element.getAttribute("name")) {
                        case "category" -> excluded |= CATEGORY.matcher(value).find();
                        case "magneturl" -> {
                            if (!value.trim().isEmpty()) magnet = value;
                        }
                        case "seeders" -> seeds = count(value);
                        default -> { }
                    }
                }
                String hash = infoHash(magnet);
                if (!excluded && !hash.isEmpty()) result.add(new Release(title, magnet, null, seeds, hash));
            }
            return ordered(result);
        } catch (javax.xml.parsers.ParserConfigurationException | org.xml.sax.SAXException error) {
            throw new IOException("Некорректный ответ JacRed", error);
        }
    }

    static List<Release> ordered(List<Release> releases) {
        var unique = new LinkedHashMap<String, Release>();
        releases.stream().sorted(Comparator.comparingInt(Release::seeders).reversed()
                .thenComparing(release -> release.torrent() == null))
                .forEach(release -> unique.merge(release.key(), release, PublicIndex::merge));
        return Collections.unmodifiableList(new ArrayList<>(unique.values()));
    }

    private static Release merge(Release best, Release alternate) {
        if (best.torrent() == null && alternate.torrent() != null) {
            return new Release(alternate.title(), best.magnet(), alternate.torrent(), best.seeders(), alternate.reference());
        }
        return best;
    }

    static String infoHash(String magnet) {
        if (magnet == null || !magnet.regionMatches(true, 0, "magnet:?", 0, 8) || magnet.length() > 8192) return "";
        Set<String> hashes = new LinkedHashSet<>();
        for (String pair : magnet.substring(8).split("&")) {
            if (!pair.regionMatches(true, 0, "xt=", 0, 3)) continue;
            try {
                String value = URLDecoder.decode(pair.substring(3), "UTF-8");
                Matcher match = INFO_HASH.matcher(value);
                if (!match.matches()) continue;
                String digest = match.group(1).toLowerCase(Locale.ROOT);
                hashes.add(digest.length() == 32 ? base32ToHex(digest) : digest);
            } catch (IllegalArgumentException ignored) {
                return "";
            } catch (UnsupportedEncodingException impossible) {
                throw new AssertionError(impossible);
            }
        }
        return hashes.size() == 1 ? hashes.iterator().next() : "";
    }

    private static String base32ToHex(String value) {
        int buffer = 0;
        int bits = 0;
        StringBuilder hex = new StringBuilder(40);
        for (int i = 0; i < value.length(); i++) {
            buffer = (buffer << 5) | "abcdefghijklmnopqrstuvwxyz234567".indexOf(value.charAt(i));
            bits += 5;
            if (bits >= 8) {
                bits -= 8;
                int next = buffer >> bits & 255;
                hex.append(Character.forDigit(next >> 4, 16)).append(Character.forDigit(next & 15, 16));
            }
        }
        return hex.toString();
    }

    static List<String> words(String input) {
        Matcher matcher = WORD.matcher(Normalizer.normalize(input, Normalizer.Form.NFKC)
                .toLowerCase(Locale.ROOT).replace('ё', 'е'));
        var result = new ArrayList<String>();
        while (matcher.find()) result.add(matcher.group());
        return result;
    }

    private static String childValue(Element parent, String tag) {
        for (Node node = parent.getFirstChild(); node != null; node = node.getNextSibling()) {
            if (node instanceof Element element && tag.equals(element.getTagName())) return element.getTextContent();
        }
        return "";
    }

    private static String firstNonNull(String first, String second, String third) {
        return first != null ? first : second != null ? second : third;
    }

    private static int count(String value) {
        String digits = value.replaceAll("[\\s\\u00a0]", "");
        return digits.matches("[0-9]{1,9}") ? Integer.parseInt(digits) : 0;
    }

    private static String htmlText(String raw) {
        return unescape(raw.replaceAll("<[^>]+>", "")).trim().replaceAll("\\s+", " ");
    }

    private static String unescape(String raw) {
        Matcher matcher = ENTITY.matcher(raw);
        StringBuffer decoded = new StringBuffer();
        while (matcher.find()) {
            String name = matcher.group(1);
            String value = switch (name) {
                case "amp" -> "&";
                case "quot" -> "\"";
                case "apos" -> "'";
                case "lt" -> "<";
                case "gt" -> ">";
                case "nbsp" -> "\u00a0";
                default -> decodeCodePoint(name, matcher.group());
            };
            matcher.appendReplacement(decoded, Matcher.quoteReplacement(value));
        }
        return matcher.appendTail(decoded).toString();
    }

    private static String decodeCodePoint(String entity, String original) {
        try {
            boolean hexadecimal = entity.startsWith("#x");
            return new String(Character.toChars(Integer.parseInt(entity.substring(hexadecimal ? 2 : 1), hexadecimal ? 16 : 10)));
        } catch (IllegalArgumentException error) {
            return original;
        }
    }

    private static final class HttpFailure extends IOException {
        final int code;
        HttpFailure(int code, String host) {
            super(host + " вернул ошибку " + code);
            this.code = code;
        }
    }
}
