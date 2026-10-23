<?php

/**
 * @file plugins/generic/archiveSync/ArchiveSyncPlugin.php
 *
 * @class ArchiveSyncPlugin
 *
 * @brief Notifies the SCH Archive when an article is published or unpublished.
 *
 * Install once per OJS installation; it covers every journal in it. It only
 * sends a small signed notification ("article X changed"). The archive then
 * fetches the metadata and PDFs itself through the REST API, so publishing in
 * OJS stays fast and nothing breaks if the archive is offline (its nightly
 * reconcile catches anything missed).
 *
 * Configure by adding to config.inc.php:
 *
 *   [archivesync]
 *   webhook_url = "https://sch.org/hooks/ojs/1"
 *   webhook_secret = "..."
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
        if ($success && Application::isInstalled() && $this->isConfigured()) {
            Hook::add('Publication::publish', $this->onPublicationChange(...));
            Hook::add('Publication::unpublish', $this->onPublicationChange(...));
        }
        return $success;
    }

    public function getDisplayName()
    {
        return 'Archive Sync';
    }

    public function getDescription()
    {
        return 'Sends a signed notification to the SCH Archive whenever an article is published or unpublished, so it can preserve the metadata and PDF. Configured in config.inc.php under [archivesync].';
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
