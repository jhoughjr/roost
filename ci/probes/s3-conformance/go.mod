module s3conformance

go 1.24

// This is the version Forgejo 15.0.7 carries. Hold it in step with the forge, and not with `mc`,
// because the two disagree about what a server has to answer.
require github.com/minio/minio-go/v7 v7.0.98
