<?php

/**
 * @file plugins/generic/archiveSync/ArchiveSyncPlugin.php
 *
 * @class ArchiveSyncPlugin
 *
 * @brief Connects OJS to the archive (doi-services.com).
 *
 * Install once per OJS installation; it covers every journal in it.
 *
 * 1. On publish/unpublish it sends a small signed notification ("article X
 *    changed"). The archive then fetches the metadata and PDFs itself through
 *    the REST API, so publishing in OJS stays fast and nothing breaks if the
 *    archive is offline (its nightly reconcile catches anything missed).
 * 2. The archive sends back each article's permanent ID (signed with the same
 *    secret) to /index.php/{journal}/archivesync/identifiers. It is stored on
 *    the publication as "archiveId" and shown on the article page as
 *    "DOI: 1003/165" (linked), right after the keywords. It is inserted into the
 *    finished page rather than through a theme hook, so it works with any theme.
 *
 * Configure by adding to config.inc.php:
 *
 *   [archivesync]
 *   webhook_url = "https://doi-services.com/hooks/ojs/1"
 *   webhook_secret = "..."
 *   ; optional, the label shown before the ID on the article page
 *   label = "DOI"
 */

namespace APP\plugins\generic\archiveSync;

use APP\core\Application;
use PKP\config\Config;
use PKP\plugins\GenericPlugin;
use PKP\plugins\Hook;

class ArchiveSyncPlugin extends GenericPlugin
{
    /** Events collected during this request, sent together at the end */
    private array $events = [];

    private bool $flushRegistered = false;

    /** @var array<int, ?string> */
    private array $contextPaths = [];

    /** The ID to show on the article page being rendered, set just before it is displayed */
    private ?string $pageArchiveId = null;

    public function register($category, $path, $mainContextId = null)
    {
        $success = parent::register($category, $path, $mainContextId);
        if ($success && Application::isInstalled()) {
            // Always on, so stored IDs keep loading and showing even if the config block is removed
            Hook::add('Schema::get::publication', $this->addArchiveIdToSchema(...));
            Hook::add('TemplateManager::display', $this->prepareArchiveId(...));
            if ($this->isConfigured()) {
                Hook::add('Publication::publish', $this->onPublicationChange(...));
                Hook::add('Publication::unpublish', $this->onPublicationChange(...));
                Hook::add('LoadHandler', $this->loadHandler(...));
            }
        }
        return $success;
    }

    public function getDisplayName()
    {
        return 'Archive Sync';
    }

    public function getDescription()
    {
        return 'Sends a signed notification to DOI Services (doi-services.com) whenever an article is published or unpublished, so it can preserve the metadata and PDF. Configured in config.inc.php under [archivesync].';
    }

    /** Applies to the whole installation, not one journal. */
    public function isSitePlugin()
    {
        return true;
    }

    private function isConfigured(): bool
    {
        return (bool) Config::getVar('archivesync', 'webhook_url') && (bool) Config::getVar('archivesync', 'webhook_secret');
    }

    /** Lets OJS store, load and return (REST API) the archive ID on every publication. */
    public function addArchiveIdToSchema(string $hookName, array $args): bool
    {
        $schema = &$args[0];
        $schema->properties->archiveId = (object) [
            'type' => 'string',
            'apiSummary' => true,
            'validation' => ['nullable', 'max:255'],
        ];
        return Hook::CONTINUE;
    }

    /** Routes /index.php/{journal}/archivesync/identifiers to ArchiveSyncHandler. */
    public function loadHandler(string $hookName, array $args): bool
    {
        $page = $args[0];
        $op = $args[1];
        if ($page !== 'archivesync' || $op !== 'identifiers') {
            return Hook::CONTINUE;
        }
        $args[3] = new ArchiveSyncHandler();
        return Hook::ABORT;
    }

    /**
     * Before the article page is rendered: remember its ID and ask for the finished HTML.
     *
     * Hook args: [$templateMgr, &$template, &$output]
     */
    public function prepareArchiveId(string $hookName, array $args): bool
    {
        try {
            [$templateMgr, $template] = $args;
            if ($template !== 'frontend/pages/article.tpl') {
                return Hook::CONTINUE;
            }
            $archiveId = $templateMgr->getTemplateVars('publication')?->getData('archiveId');
            if ($archiveId) {
                $this->pageArchiveId = (string) $archiveId;
                $templateMgr->registerFilter('output', [$this, 'insertArchiveId']);
            }
        } catch (\Throwable $e) {
            error_log('ArchiveSync: could not show archive ID: ' . $e->getMessage());
        }
        return Hook::CONTINUE;
    }

    /**
     * Smarty output filter for the article page: adds "DOI: 1003/165" (linked) after the
     * keywords, or where they would be (after the DOI or authors), or after the title.
     * Works on the finished HTML, so it doesn't depend on the theme calling any hook.
     */
    public function insertArchiveId(string $output, $template = null): string
    {
        // Only the whole page (not fragments rendered on the way), and only once
        if ($this->pageArchiveId === null || stripos($output, '</body>') === false) {
            return $output;
        }
        $url = $this->pageArchiveId;
        $this->pageArchiveId = null;
        try {
            return self::addArchiveIdToPage($output, $url, Config::getVar('archivesync', 'label') ?: 'DOI');
        } catch (\Throwable $e) {
            error_log('ArchiveSync: could not show archive ID: ' . $e->getMessage());
            return $output;
        }
    }

    /** Pure HTML work, kept separate so it can be tested without OJS. */
    public static function addArchiveIdToPage(string $html, string $url, string $label): string
    {
        // Search inside the article only, so a sidebar keyword cloud etc. is never matched
        $from = 0;
        if (preg_match('/<article\b/i', $html, $m, PREG_OFFSET_CAPTURE)) {
            $from = $m[0][1];
        }
        $at = null;
        $tag = 'div';
        foreach (['keywords', 'doi', 'authors'] as $class) {
            $pattern = '/<(section|div)\b[^>]*\bclass="[^"]*\b' . $class . '\b[^"]*"[^>]*>/i';
            if (preg_match($pattern, $html, $m, PREG_OFFSET_CAPTURE, $from)) {
                $at = self::closingTagEnd($html, strtolower($m[1][0]), $m[0][1]);
                if ($at !== null) {
                    $tag = strtolower($m[1][0]);
                    break;
                }
            }
        }
        if ($at === null && preg_match('#</h1>#i', $html, $m, PREG_OFFSET_CAPTURE, $from)) {
            $at = $m[0][1] + strlen($m[0][0]);
        }
        if ($at === null) {
            return $html;
        }

        $shown = trim((string) parse_url($url, PHP_URL_PATH), '/') ?: $url;  // https://doi-services.com/1003/165?redirect -> 1003/165
        $e = fn (string $s) => htmlspecialchars($s, ENT_QUOTES);
        $block = "<{$tag} class=\"item doi archive_id\"><h2 class=\"label\">{$e($label)}:</h2> "
            . "<span class=\"value\"><a href=\"{$e($url)}\">{$e($shown)}</a></span></{$tag}>";
        return substr($html, 0, $at) . $block . substr($html, $at);
    }

    /** Offset just after the tag that closes the one opened at $openAt (handles nesting). */
    private static function closingTagEnd(string $html, string $tag, int $openAt): ?int
    {
        preg_match_all('#<(/?)' . $tag . '\b[^>]*>#i', $html, $tags, PREG_SET_ORDER | PREG_OFFSET_CAPTURE, $openAt);
        $depth = 0;
        foreach ($tags as $t) {
            $depth += $t[1][0] === '/' ? -1 : 1;
            if ($depth === 0) {
                return $t[0][1] + strlen($t[0][0]);
            }
        }
        return null;
    }

    /**
     * Hook args: [&$newPublication, $publication, $submission]
     */
    public function onPublicationChange(string $hookName, array $args): bool
    {
        try {
            [$newPublication, , $submission] = $args;
            $contextId = (int) $submission->getData('contextId');
            $this->events[] = [
                'event' => $hookName === 'Publication::publish' ? 'publish' : 'unpublish',
                'contextId' => $contextId,
                'contextPath' => $this->contextPath($contextId),
                'submissionId' => (int) $submission->getId(),
                'publicationId' => (int) $newPublication->getId(),
                'status' => (int) $newPublication->getData('status'),
                'at' => time(),
            ];
            if (!$this->flushRegistered) {
                // Send after OJS has finished its own work for this request (e.g. a
                // whole issue being published), as one HTTP call.
                register_shutdown_function([$this, 'flush']);
                $this->flushRegistered = true;
            }
        } catch (\Throwable $e) {
            error_log('ArchiveSync: could not record event: ' . $e->getMessage());
        }
        return Hook::CONTINUE;
    }

    private function contextPath(int $contextId): ?string
    {
        if (!array_key_exists($contextId, $this->contextPaths)) {
            $context = Application::getContextDAO()->getById($contextId);
            $this->contextPaths[$contextId] = $context?->getPath();
        }
        return $this->contextPaths[$contextId];
    }

    /** Never throws: a failed notification must not affect OJS. */
    public function flush(): void
    {
        if (!$this->events) {
            return;
        }
        $body = json_encode(['events' => $this->events], JSON_UNESCAPED_SLASHES);
        $this->events = [];
        $timestamp = (string) time();
        $signature = hash_hmac('sha256', $timestamp . '.' . $body, (string) Config::getVar('archivesync', 'webhook_secret'));

        try {
            Application::get()->getHttpClient()->request('POST', Config::getVar('archivesync', 'webhook_url'), [
                'headers' => [
                    'Content-Type' => 'application/json',
                    'X-ArchiveSync-Timestamp' => $timestamp,
                    'X-ArchiveSync-Signature' => $signature,
                ],
                'body' => $body,
                'connect_timeout' => 3,
                'timeout' => 5,
            ]);
        } catch (\Throwable $e) {
            error_log('ArchiveSync: webhook failed (the nightly reconcile will catch up): ' . $e->getMessage());
        }
    }
}
