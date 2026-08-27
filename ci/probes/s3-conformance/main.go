// A conformance probe for the S3 surface that backs Forgejo's artifact and log storage.
//
// It makes the calls Forgejo makes, with the client Forgejo carries, and names the first one that
// fails. Forgejo reports a storage fault as `Artifact service responded with 500` against whichever
// step it was running, which is usually the merge, so the message names an operation that is not the
// one at fault. This probe names the call.
//
// Run it against a candidate build before a deploy:
//
//	docker run --rm -e S3_ENDPOINT=... -e S3_ACCESS=... -e S3_SECRET=... \
//	  -v "$PWD:/src" -w /src golang:1.24 sh -c 'go mod tidy >/dev/null 2>&1; go run main.go'
//
// Pin the client to the version Forgejo carries. Versions disagree here: v7.0.90 accepts an absent
// Last-Modified on a read and v7.0.98 refuses it, so an older client passes a server that Forgejo
// cannot use.
package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type check struct {
	name string
	run  func(context.Context, *minio.Client, string) error
}

func main() {
	endpoint := env("S3_ENDPOINT", "s3.jimmyhoughjr.net")
	bucket := env("S3_BUCKET", "forgejo")

	client, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(os.Getenv("S3_ACCESS"), os.Getenv("S3_SECRET"), ""),
		Secure: env("S3_SECURE", "") == "1",
	})
	if err != nil {
		fmt.Println("the client did not build:", err)
		os.Exit(1)
	}

	payload := bytes.Repeat([]byte("x"), 549)
	key := "probe/s3-conformance/chunk.part"

	checks := []check{
		{"the bucket is there", func(ctx context.Context, c *minio.Client, b string) error {
			ok, err := c.BucketExists(ctx, b)
			if err == nil && !ok {
				return fmt.Errorf("the bucket %s is absent", b)
			}
			return err
		}},
		{"a write of known length", func(ctx context.Context, c *minio.Client, b string) error {
			_, err := c.PutObject(ctx, b, key, bytes.NewReader(payload), int64(len(payload)),
				minio.PutObjectOptions{})
			return err
		}},
		{"a head", func(ctx context.Context, c *minio.Client, b string) error {
			_, err := c.StatObject(ctx, b, key, minio.StatObjectOptions{})
			return err
		}},
		// The read is where an absent Last-Modified shows. A head keeps its own headers and passes,
		// so a probe that stops at the head reports a server that Forgejo cannot use.
		{"a read of the body", func(ctx context.Context, c *minio.Client, b string) error {
			obj, err := c.GetObject(ctx, b, key, minio.GetObjectOptions{})
			if err != nil {
				return err
			}
			_, err = io.ReadAll(obj)
			return err
		}},
		// Forgejo writes the merged artifact without knowing its length, which streams and signs
		// the body in chunks. The single write above never takes that path.
		{"a write of unknown length", func(ctx context.Context, c *minio.Client, b string) error {
			_, err := c.PutObject(ctx, b, "probe/s3-conformance/merged.gz", bytes.NewReader(payload), -1,
				minio.PutObjectOptions{ContentType: "application/octet-stream"})
			return err
		}},
		{"a delete", func(ctx context.Context, c *minio.Client, b string) error {
			return c.RemoveObject(ctx, b, key, minio.RemoveObjectOptions{})
		}},
	}

	ctx := context.Background()
	failed := 0
	for _, c := range checks {
		if err := c.run(ctx, client, bucket); err != nil {
			fmt.Printf("FAIL  %-26s %s\n", c.name, strings.ReplaceAll(err.Error(), "\n", " "))
			failed++
			continue
		}
		fmt.Printf("ok    %s\n", c.name)
	}
	if failed > 0 {
		fmt.Printf("\n%d of %d checks failed. Forgejo cannot use this endpoint.\n", failed, len(checks))
		os.Exit(1)
	}
	fmt.Printf("\nall %d checks passed.\n", len(checks))
}

func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
