#!/usr/bin/env bash

# Exit on error
set -e

# Record directory of script for convenience
SCRIPT_PATH=$(readlink -f "${0}")
SCRIPT_DIR=$(dirname "${SCRIPT_PATH}")

# Prepare stuff
source "${SCRIPT_DIR}"/../prepare/make_it_so.envsh

# Check whether the required vars are set
: ${SOURCE_JOB_NAME?'SOURCE_JOB_NAME is required for this script to function'}
: ${SOURCE_JOB_BUILD_NUMBER?'SOURCE_JOB_BUILD_NUMBER is required for this script to function'}
: ${DEPLOY_GIT_COMMIT?'DEPLOY_GIT_COMMIT is required for this script to function'}
: ${DEPLOY_GIT_URL?'DEPLOY_GIT_URL is required for this script to function'}
: ${AERIUS_REGISTRY_URL?'AERIUS_REGISTRY_URL is required for this script to function'}
: ${DEPLOY_IMAGE_TAG?'DEPLOY_IMAGE_TAG is required for the script to function'}
: ${SERVICE_TYPE?'SERVICE_TYPE is required for this script to function'}
# SERVICE_THEME is optional, so we do not check on it
# FLAGS is optional, so we do not check on it
: ${AWS_ACCOUNT_NAME?'AWS_ACCOUNT_NAME is required for this script to function'}
: ${DEPLOY_TERRAFORM_ACTION:=apply}

echo "[terraform/run] # Going to do a terraform build for: ${SOURCE_JOB_NAME} #${SOURCE_JOB_BUILD_NUMBER}"

cd "${WORKSPACE}"

# Generate a short environment name
# matches: *[year]*
if [[ "${SOURCE_JOB_NAME}" =~ [0-9]{4} ]]; then
  ENV_NAME_SHORT=$(sed -E 's#(.{3}).*([0-9]{4}.*)#\1\2#' <<< "${SOURCE_JOB_NAME}") # CALCULATOR1970-DEV becomes CAL1970-DEV
# matches: *-*-* (like register-opendata)
elif [[ "${SOURCE_JOB_NAME}" == *-*-* ]]; then
  ENV_NAME_SHORT=$(sed -E 's#(.{1}).*-(.{2}).*(-.*)#\1\2\3#' <<< "${SOURCE_JOB_NAME}") # REGISTER-OPENDATA-DEV becomes ROP-DEV
# For PR's we'll do something else
# matches: *-PR
elif [[ "${SOURCE_JOB_NAME}" == *-PR ]]; then
  ENV_NAME_SHORT=$(sed -E 's#(.{3}).*(-.*)#\1#' <<< "${SOURCE_JOB_NAME}")"-PR${SOURCE_JOB_BUILD_NUMBER}" # AERIUS-II-PR (with build number 119) becomes AER-PR119
# Fallback
else
  ENV_NAME_SHORT=$(sed -E 's#(.{3}).*(-.*)#\1\2#' <<< "${SOURCE_JOB_NAME}") # CALCULATOR-DEV becomes CAL-DEV
fi
# Let's make an exception if it's PRERELEASE, we should shorten that part as well
[[ "${ENV_NAME_SHORT}" == *-PRERELEASE ]] && ENV_NAME_SHORT="${ENV_NAME_SHORT//-PRERELEASE}-PRE"

# Determine deploy webhost: DEPLOY_WEBHOST_SUBDOMAIN / DEPLOY_WEBHOST_DOMAIN / DEPLOY_WEBHOST_TLD / DEPLOY_WEBHOST
source "${SCRIPT_DIR}"/../common/determine_deploy_webhost.envsh

# Defaults
ENV_NAME_UPPERCASE="${SOURCE_JOB_NAME}"
ENV_NAME="${SOURCE_JOB_NAME,,}"
AWS_REGION='eu-west-1'
COGNITO_USER_POOL_NAME='nl-dev'
COGNITO_USER_POOL_DOMAIN='auth-dev.aerius.nl'
APP_TIMEZONE='Europe/Amsterdam'

# If PR deploy, naming convention is a bit different
[[ "${ENV_NAME_UPPERCASE}" == *-PR ]] && \
  # CALCULATOR-PR (with build number 119) becomes CALCULATOR-PR119
  ENV_NAME="${ENV_NAME}${SOURCE_JOB_BUILD_NUMBER}" \
  ENV_NAME_UPPERCASE="${ENV_NAME_UPPERCASE}${SOURCE_JOB_BUILD_NUMBER}"

# Determine AWS region to deploy for based on AWS_ACCOUNT_NAME
# Also determine cognito settings and update TLD to use
# If UK deploy, we need to use other settings
[[ "${AWS_ACCOUNT_NAME}" == UK-* ]] \
  && AWS_REGION='eu-west-2' \
  && COGNITO_USER_POOL_NAME='uk-dev' \
  && COGNITO_USER_POOL_DOMAIN='auth-dev.aerius.uk' \
  && APP_TIMEZONE='Europe/London'
COGNITO_CALLBACK_DOMAIN="${DEPLOY_WEBHOST}"

# Set some convenience variables
ENV_ROOT_DIR="environments/${ENV_NAME}"
ECR_REPO=$(cut -d '/' -f1 <<< "${AERIUS_REGISTRY_URL}")
ECR_DIRECTORY=$(cut -d '/' -f2- <<< "${AERIUS_REGISTRY_URL}")
ECR_DIRECTORY="${ECR_DIRECTORY%%/}" # Remove slash at end if any
PRODUCT_NAME=$(cut -d '/' -f2 <<< "${DEPLOY_GIT_URL}")
PRODUCT_NAME="${PRODUCT_NAME%%.git}"
PRODUCT_NAME="${PRODUCT_NAME##aerius-}" # Remove aerius- if it starts with that

if [[ -d "${ENV_ROOT_DIR}" ]]; then
  echo '[terraform/run] # Environment directory "'"${ENV_ROOT_DIR}"'" already exists, quitting..'
  exit 1
fi

# Create environment root dir + region dir
mkdir -p "${ENV_ROOT_DIR}/${AWS_REGION}"

# Write modules.json
cat << EOF > "${ENV_ROOT_DIR}/modules.json"
[
  {
    "url": "${DEPLOY_GIT_URL}",
    "version": "${DEPLOY_GIT_COMMIT}",
    "checkout_path": "terraform"
  }
]
EOF

# Get environment modules
TERRAFORM_ENVIRONMENT="${ENV_NAME}" \
AWS_REGION="${AWS_REGION}" \
  scripts/prepare-modules.sh

# Write region.terragrunt.hcl - for now all our regions contains 3 AZ's, so this assumption is safe - let's automatically fetch it in the future
cat << EOF > "${ENV_ROOT_DIR}/region.terragrunt.hcl"
locals {
    aws_region         = "${AWS_REGION}"
    availability_zones = ["${AWS_REGION}a", "${AWS_REGION}b", "${AWS_REGION}c"]
}
EOF

# Get AWS account ID
AWS_ACCOUNT_ID=$(scripts/aws-account-name-to-id.sh "${AWS_ACCOUNT_NAME}")

# Write account.terragrunt.hcl
cat << EOF > "${ENV_ROOT_DIR}/account.terragrunt.hcl"
locals {
  account_id        = "${AWS_ACCOUNT_ID}"
}
EOF

# Write start of environment.terragrunt.hcl
cat << EOF > "${ENV_ROOT_DIR}/environment.terragrunt.hcl"
locals {
  service = {
    "code"  = "${ENV_NAME_UPPERCASE}"
    "type"  = "${SERVICE_TYPE}"
    "theme" = "${SERVICE_THEME}"
  }
  environment                 = "${ENV_NAME_UPPERCASE}"
  environment_short           = "${ENV_NAME_SHORT}"

  tf_bucket_key_prefix        = "environments/${ENV_NAME}/"
  app_version                 = "${DEPLOY_IMAGE_TAG}"
  app_timezone                = "${APP_TIMEZONE}"

  ecr_repo                    = "${ECR_REPO}"
  ecr_directory               = "${ECR_DIRECTORY}"

  shared_basicinfra           = true
  ecs_disable_rolling_updates = true

EOF

declare -A FLAG_SETTINGS
# Add some entries as flags, so they can be overridden if needed
FLAG_SETTINGS[COGNITO_ENABLED]=true
FLAG_SETTINGS[COGNITO_USER_POOL_NAME]="${COGNITO_USER_POOL_NAME}"
FLAG_SETTINGS[COGNITO_CALLBACK_DOMAIN]="${COGNITO_CALLBACK_DOMAIN}"
FLAG_SETTINGS[COGNITO_USER_POOL_DOMAIN]="${COGNITO_USER_POOL_DOMAIN}"
FLAG_SETTINGS[SCALE_BASED_ON_CRON]=false
FLAG_SETTINGS[RDS_COUNT]=0
FLAG_SETTINGS[S3_BACKUP_ENABLED]=false

# If there is a product specific dynamic configuration, run it and add it environment.terragrunt.hcl
PRODUCT_SPECIFIC_DYNAMIC_SCRIPT_DIR="${ENV_ROOT_DIR}/${AWS_REGION}/env.d"
if [[ -d "${PRODUCT_SPECIFIC_DYNAMIC_SCRIPT_DIR}" ]]; then
  # Loop over the scripts
  for PRODUCT_SPECIFIC_DYNAMIC_SCRIPT in ${PRODUCT_SPECIFIC_DYNAMIC_SCRIPT_DIR}/*.sh; do
    # If it's not executable, crash
    ! [[ -x "${PRODUCT_SPECIFIC_DYNAMIC_SCRIPT}" ]] \
      && echo '[terraform/run] # The product specific dynamic script found ('"${PRODUCT_SPECIFIC_DYNAMIC_SCRIPT}"') is not executable, crashing..' \
      && exit 1

    echo '[terraform/run] # Running product specific dynamic script: '"${PRODUCT_SPECIFIC_DYNAMIC_SCRIPT}"
    echo '  # Adding entries for: '"${PRODUCT_SPECIFIC_DYNAMIC_SCRIPT}" >> "${ENV_ROOT_DIR}/environment.terragrunt.hcl"
    ENV_NAME="${ENV_NAME}" \
    DEPLOY_WEBHOST="${DEPLOY_WEBHOST}" \
      ./"${PRODUCT_SPECIFIC_DYNAMIC_SCRIPT}" >> "${ENV_ROOT_DIR}/environment.terragrunt.hcl"
  done
fi

# Process flags specified
source "${SCRIPT_DIR}"/../common/read_job_flags.envsh

# Write flag settings to environment.terragrunt.hcl
echo '  # Adding flag settings (if any)' >> "${ENV_ROOT_DIR}/environment.terragrunt.hcl"
for FLAG_SETTING_KEY in "${!FLAG_SETTINGS[@]}"; do
  FLAG_SETTING_VALUE="${FLAG_SETTINGS[$FLAG_SETTING_KEY]}"
  # Use lowercase key for Terraform
  FLAG_SETTINGS_KEY="${FLAG_SETTING_KEY,,}"

  echo '# Adding flag setting: '"${FLAG_SETTINGS_KEY}"
  # if multiline value, use heredoc
  if [[ ${FLAG_SETTING_VALUE} == *$'\n'* ]]; then
    echo "  ${FLAG_SETTINGS_KEY}"' = <<ENDOFVARIABLE
'"${FLAG_SETTING_VALUE}"'
ENDOFVARIABLE
' >> "${ENV_ROOT_DIR}/environment.terragrunt.hcl"
  # if a raw value is required, because the use of a set/list/map is needed, process it like that.
  # This is required as Bash doesn't support multidimensional arrays.
  # Format will be: %% RAWVALUE <my RAW value> %%
  # You will be responsible for the exact formatting used,
  #  so please think twice about what you are about to do and then think again for good measure.
  elif [[ ${FLAG_SETTING_VALUE} == '%% RAWVALUE '* ]] && [[ ${FLAG_SETTING_VALUE} == *' %%' ]]; then
    FLAG_SETTING_VALUE="${FLAG_SETTING_VALUE#'%% RAWVALUE '}"
    FLAG_SETTING_VALUE="${FLAG_SETTING_VALUE%' %%'}"
    echo "  ${FLAG_SETTINGS_KEY}"' = '"${FLAG_SETTING_VALUE}" >> "${ENV_ROOT_DIR}/environment.terragrunt.hcl"
  else
    echo "  ${FLAG_SETTINGS_KEY}"' = "'"${FLAG_SETTING_VALUE}"'"' >> "${ENV_ROOT_DIR}/environment.terragrunt.hcl"
  fi
done

# Write end of environment.terragrunt.hcl
cat << EOF >> "${ENV_ROOT_DIR}/environment.terragrunt.hcl"
}
EOF

echo '# Actual files generated'
echo '# modules.json'
cat "${ENV_ROOT_DIR}/modules.json"
echo '# region.terragrunt.hcl'
cat "${ENV_ROOT_DIR}/region.terragrunt.hcl"
echo '# account.terragrunt.hcl'
cat "${ENV_ROOT_DIR}/account.terragrunt.hcl"
echo '# environment.terragrunt.hcl'
cat "${ENV_ROOT_DIR}/environment.terragrunt.hcl"

# Do the real terragrunt action if it's not a dry run
if [[ "${DEPLOY_TERRAFORM_ACTION}" != 'dry-run' ]]; then
  # Do a init first so everything is fetched and cached.
  # While not required, it cleans up the log and we seem to be hitting some race-condition like issues
  #  which I hope will be history with this included.
  TERRAFORM_ENVIRONMENT="${ENV_NAME}" \
  TERRAFORM_ACTION='init' \
  TERRAFORM_COMPONENT=application_services \
    scripts/do-terragrunt-action.sh

  TERRAFORM_ENVIRONMENT="${ENV_NAME}" \
  TERRAFORM_ACTION="${DEPLOY_TERRAFORM_ACTION}" \
  TERRAFORM_COMPONENT=application_services \
    scripts/do-terragrunt-action.sh

  if [[ "${DEPLOY_TERRAFORM_ACTION}" == 'apply' && -n "${CICD_QA_PAIR_TARGET:-}" ]]; then
    AWS_PROFILE="${AWS_ACCOUNT_NAME,,}" AWS_REGION="${AWS_REGION}" \
      python3 "${CICD_SCRIPTS_DIR}/qa/qa_pair.py" capture --root "${WORKSPACE}"
  fi
fi
