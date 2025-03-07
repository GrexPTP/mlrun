(artifacts)=
# Artifacts

An artifact is any data that is produced and/or consumed by functions, jobs, or pipelines. 

Artifacts metadata is stored in the project’s database. The main types of artifacts are:

- Files — files, directories, images, figures, and plotlines
- Datasets — any data, such as tables and DataFrames
- Models — all trained models
- Feature Store Objects — Feature sets and feature vectors

**In this section**
- [Viewing artifacts](#viewing-artifacts)
- [Artifact path](#artifact-path)
- [Saving artifacts in run-specific paths](#saving-artifacts-in-run-specific-paths)
- [Artifact URIs, versioning, and metadata](#artifact-uris-versioning-and-metadata)
- [Customizing the allowed paths](#customizing-the-allowed-paths)

**See also:**
- {ref}`working-with-data-and-model-artifacts`
- {ref}`models`
- {ref}`logging_datasets`

## Viewing artifacts

Artifacts that are stored in certain paths (see [Artifact path](#artifact-path)) can be viewed and managed in the UI. 
In the **Project** page, select the type of artifact you want to view from the left-hand menu: 
Feature Store (for feature-sets, feature-vectors and features), Datasets, Artifacts, or Models.

Example dataset artifact screen:
<br><br>
<img src="../_static/images/dataset_artifact.png" alt="projects-artifacts" width="800"/>

Artifacts that were generated by an MLRun job can also be viewed from the **Jobs > Artifacts** tab.

You can search the artifacts based on time and labels, and you can filter the artifacts by tag type.
For each artifact, you can view its content, its location, the artifact type, labels, 
the producer of the artifact, the artifact owner, last update date, and type-specific information.
You can download the artifact. You can also tag and remove tags from artifacts using the UI.


## Artifact path

Any path that is supported by MLRun can be used to store artifacts. However, only artifacts that are stored in paths that are 
system-configured as "allowed" in the MLRun service are visible in the UI. These are:
- MLRun < 1.2: The allowed paths include only v3io paths
- MLRun 1.2 and higher: Allows cloud storage paths &mdash; `v3io://`, `s3://`, `az://`, `gcs://`, `gs:// `. `http://` paths are not visible
 due to security reasons.
- MLRun 1.5 adds support for  dbfs (Databricks file system): `dbfs://`

Jobs use the default or job specific `artifact_path` parameter to determine where the artifacts are stored.
The default `artifact_path` can be specified at the cluster level, client level, project level, or job level 
(at that precedence order), or can be specified as a parameter in the specific `log` operation.

You can set the default `artifact_path` for your environment using the {py:func}`~mlrun.set_environment` function.

You can override the default `artifact_path` configuration by setting the `artifact_path` parameter of 
the {py:func}`~mlrun.set_environment` function. You can use variables in the artifacts path, 
such as `{{project}}` for the name of the running project or `{{run.uid}}` for the current job/pipeline run UID. 
(The default artifacts path uses `{{project}}`.) The following example configures the artifacts path to an 
artifacts directory in the current active directory (`./artifacts`)

    set_environment(artifact_path='./artifacts')

```{admonition} For Iguazio MLOps Platform users
In the platform, the default artifacts path is a <project name>/artifacts directory in the 
predefined “projects” data container: `/v3io/projects/<project name>/artifacts`
(for example, `/v3io/projects/myproject/artifacts` for a “myproject” project).
```

## Saving artifacts in run-specific paths
When you specify `{{run.uid}}`, the artifacts for each job are stored in a dedicated directory for each executed job.
Under the artifact path, you should see the source-data file in a new directory whose name is derived from the unique run ID.
Otherwise, the same artifacts directory is used in all runs, and the artifacts for newer runs override those from the previous runs.

As previously explained, `set_environment` returns a tuple with the project name and artifacts path.
You can optionally save your environment's artifacts path to a variable, as demonstrated in the previous steps.
You can then use the artifacts-path variable to extract paths to task-specific artifact subdirectories.
For example, the following code extracts the path to the artifacts directory of a `training` task, and saves the path 
to a `training_artifacts` variable:

```python
from os import path
training_artifacts = path.join(artifact_path, 'training')
```

```{admonition} Note
The artifacts path uses [data store URLs](./datastore.html#shared-data-stores), which are not necessarily local file paths 
(for example, `s3://bucket/path`). Be careful not to use such paths with general file utilities.
```

## Artifact URIs, versioning, and metadata

Artifacts have unique URIs in the form `store://<type>/<project>/<key/path>[:tag]`. 
The URI is automatically generated by `log_artifact` and can be used as input to jobs, functions, pipelines, etc.

Artifacts are versioned. Each unique version has a unique IDs (uid) and can have a `tag` label.  
When the tag is not specified, it uses the `latest` version. 

Artifact metadata and objects can be accessed through the SDK or downloaded from the UI (as YAML files). 
They host common and object specific metadata such as:

* Common metadata: name, project, updated, version info
* How they were produced (user, job, pipeline, etc.)
* Lineage data (sources used to produce that artifact)
* Information about formats, schema, sample data 
* Links to other artifacts (e.g. a model can point to a chart)
* Type-specific attributes

Artifacts can be obtained via the SDK through type specific APIs or using generic artifact APIs such as:
* {py:func}`~mlrun.run.get_dataitem` - get the {py:class}`~mlrun.datastore.DataItem` object for reading/downloading the artifact content
* {py:func}`~mlrun.datastore.get_store_resource` - get the artifact object

Example artifact URLs:

    store://artifacts/default/my-table
    store://artifacts/sk-project/train-model:e95f757e-7959-4d66-b500-9f6cdb1f0bc7
    store://feature-sets/stocks/quotes:v2
    store://feature-vectors/stocks/enriched-ticker
    

<!-- ## Dataset artifacts moved to data coll and prep, model and plot artifacts to working-with-data-and-model-artifacts -->


[Back to top](#top)