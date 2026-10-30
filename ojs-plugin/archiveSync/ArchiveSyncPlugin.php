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
 *    the publication as "archiveId" and shown on the article page.
 *
 * Configure by adding to config.inc.php:
 *
 *   [archivesync]
 *   webhook_url = "https://doi-services.com/hooks/ojs/1"
 *   webhook_secret = "..."
 *   ; optional, the heading shown above the ID on the article page
 *   label = "Permanent link"
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

    public function register($category, $path, $mainContextId = null)
    {
        $success = parent::register($category, $path, $mainContextId);
        if ($success && Application::isInstalled()) {
            // Always on, so stored IDs keep loading and showing even if the config block is removed
            Hook::add('Schema::get::publication', $this->addArchiveIdToSchema(...));
            Hook::add('Templates::Article::Details', $this->showArchiveId(...));
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
     * Adds the ID to the article page (default theme and themes based on it).
     *
     * Hook args: [$params, $smarty, &$output]
     */
    public function showArchiveId(string $hookName, array $args): bool
    {
        try {
            $templateMgr = $args[1];
            $output = &$args[2];
            $publication = $templateMgr->getTemplateVars('publication');
            $archiveId = $publication?->getData('archiveId');
            if (!$archiveId) {
                return Hook::CONTINUE;
            }
            $label = Config::getVar('archivesync', 'label') ?: 'Permanent link';
            $url = htmlspecialchars($archiveId, ENT_QUOTES);
            $output .= '<section class="item archive_id"><h2 class="label">' . htmlspecialchars($label, ENT_QUOTES)
                . '</h2><div class="value"><a href="' . $url . '">' . $url . '</a></div></section>';
        } catch (\Throwable $e) {
            error_log('ArchiveSync: could not show archive ID: ' . $e->getMessage());
        }
        return Hook::CONTINUE;
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
