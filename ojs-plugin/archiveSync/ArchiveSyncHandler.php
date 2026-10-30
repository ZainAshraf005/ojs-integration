<?php

/**
 * @file plugins/generic/archiveSync/ArchiveSyncHandler.php
 *
 * @class ArchiveSyncHandler
 *
 * @brief Receives article IDs from the archive and stores them in OJS.
 *
 * POST /index.php/{journal}/archivesync/identifiers
 *   {"items": [{"submissionId": 188, "identifier": "https://doi-services.com/1001/188?redirect"}, ...]}
 *
 * Signed exactly like the plugin's webhooks (same secret), so no OJS login or
 * API key is involved. The REST API can't be used for this: OJS refuses to
 * edit a publication once it is published.
 */

namespace APP\plugins\generic\archiveSync;

use APP\facades\Repo;
use APP\handler\Handler;
use PKP\config\Config;

class ArchiveSyncHandler extends Handler
{
    private const MAX_SKEW = 300;
    private const MAX_ITEMS = 200;

    /** Access is checked by the request signature, not by OJS roles. */
    public function authorize($request, &$args, $roleAssignments)
    {
        return true;
    }

    public function identifiers($args, $request)
    {
        if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
            return $this->reply(405, ['error' => 'POST only']);
        }

        $body = (string) file_get_contents('php://input');
        $timestamp = (string) ($_SERVER['HTTP_X_ARCHIVESYNC_TIMESTAMP'] ?? '');
        $signature = (string) ($_SERVER['HTTP_X_ARCHIVESYNC_SIGNATURE'] ?? '');
        if (!ctype_digit($timestamp) || abs(time() - (int) $timestamp) > self::MAX_SKEW) {
            return $this->reply(401, ['error' => 'stale or missing timestamp']);
        }
        $expected = hash_hmac('sha256', $timestamp . '.' . $body, (string) Config::getVar('archivesync', 'webhook_secret'));
        if (!hash_equals($expected, $signature)) {
            return $this->reply(401, ['error' => 'bad signature']);
        }

        $items = json_decode($body, true)['items'] ?? null;
        $context = $request->getContext();
        if (!is_array($items) || !$context) {
            return $this->reply(400, ['error' => 'expected {"items": [...]} on a journal URL']);
        }

        $stored = [];
        $skipped = [];
        foreach (array_slice($items, 0, self::MAX_ITEMS) as $item) {
            $submissionId = (int) ($item['submissionId'] ?? 0);
            $identifier = (string) ($item['identifier'] ?? '');
            try {
                if (!preg_match('#^https?://#', $identifier) || strlen($identifier) > 255
                    || !filter_var($identifier, FILTER_VALIDATE_URL)) {
                    $skipped[] = ['submissionId' => $submissionId, 'reason' => 'invalid identifier'];
                    continue;
                }
                $submission = $submissionId ? Repo::submission()->get($submissionId) : null;
                if (!$submission || (int) $submission->getData('contextId') !== (int) $context->getId()) {
                    $skipped[] = ['submissionId' => $submissionId, 'reason' => 'no such article in this journal'];
                    continue;
                }
                // Every version, so it also shows on older versions' pages
                foreach ($submission->getData('publications') as $publication) {
                    if ($publication->getData('archiveId') !== $identifier) {
                        Repo::publication()->edit($publication, ['archiveId' => $identifier]);
                    }
                }
                $stored[] = $submissionId;
            } catch (\Throwable $e) {
                error_log('ArchiveSync: could not store ID for submission ' . $submissionId . ': ' . $e->getMessage());
                $skipped[] = ['submissionId' => $submissionId, 'reason' => 'error: ' . $e->getMessage()];
            }
        }
        return $this->reply(200, ['stored' => $stored, 'skipped' => $skipped]);
    }

    private function reply(int $status, array $data): void
    {
        http_response_code($status);
        header('Content-Type: application/json');
        header('Cache-Control: no-store');
        echo json_encode($data, JSON_UNESCAPED_SLASHES);
    }
}
