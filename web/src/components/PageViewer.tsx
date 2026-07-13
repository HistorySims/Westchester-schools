"use client";

interface PageViewerProps {
  imageUrl: string | null;
  resourceUrl: string | null;
  className?: string;
}

function deriveLocPageUrl(imageUrl: string): string {
  const legacy = imageUrl.match(
    /^https:\/\/chroniclingamerica\.loc\.gov\/lccn\/([^/]+)\/([^/]+)\/(ed-\d+)\/(seq-\d+)\.(?:jpg|jp2|pdf)$/
  );
  if (legacy) {
    return `https://www.loc.gov/resource/${legacy[1]}/${legacy[2]}/${legacy[3]}/${legacy[4]}/`;
  }
  return imageUrl;
}

function bestLinkUrl(
  resourceUrl: string | null,
  imageUrl: string | null
): string {
  if (resourceUrl && resourceUrl.trim()) return resourceUrl;
  if (imageUrl) return deriveLocPageUrl(imageUrl);
  return "https://www.loc.gov/";
}

export function PageViewer({
  imageUrl,
  resourceUrl,
  className = "",
}: PageViewerProps) {
  if (!imageUrl) {
    return (
      <div className={`relative h-full bg-stone-900 ${className}`}>
        <div className="absolute inset-0 flex items-center justify-center">
          <p className="text-stone-500 text-sm">
            Click a citation to view the original page
          </p>
        </div>
      </div>
    );
  }

  const url = bestLinkUrl(resourceUrl, imageUrl);

  return (
    <div className={`relative h-full bg-stone-900 ${className}`}>
      <div className="absolute inset-0 flex items-center justify-center px-6">
        <div className="text-center max-w-sm">
          <div className="mb-4 text-stone-600">
            <svg
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.5}
              stroke="currentColor"
              className="w-16 h-16 mx-auto"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 7.5h1.5m-1.5 3h1.5m-7.5 3h7.5m-7.5 3h7.5m3-9h3.375c.621 0 1.125.504 1.125 1.125V18a2.25 2.25 0 0 1-2.25 2.25M16.5 7.5V18a2.25 2.25 0 0 0 2.25 2.25M16.5 7.5V4.875c0-.621-.504-1.125-1.125-1.125H4.125C3.504 3.75 3 4.254 3 4.875V18a2.25 2.25 0 0 0 2.25 2.25h13.5M6 7.5h3v3H6v-3Z"
              />
            </svg>
          </div>
          <h3 className="text-stone-300 text-base font-serif mb-2">
            View this page on the Library of Congress
          </h3>
          <p className="text-stone-500 text-xs mb-5 leading-relaxed">
            LOC&apos;s Cloudflare protection blocks inline embedding.
            The first page you open may show a brief
            &ldquo;verify you&apos;re human&rdquo; check.
          </p>
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block px-5 py-2.5 bg-amber-800 text-amber-50 text-sm font-medium rounded hover:bg-amber-700 active:bg-amber-900 transition-colors"
          >
            Open on Library of Congress
          </a>
        </div>
      </div>
    </div>
  );
}
