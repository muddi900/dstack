import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cached_property
from typing import Dict, List, Optional, Set

import oci

from dstack._internal.core.backends.base.compute import Compute, get_instance_name, get_user_data
from dstack._internal.core.backends.base.offers import get_catalog_offers
from dstack._internal.core.backends.oci import resources
from dstack._internal.core.backends.oci.config import OCIConfig
from dstack._internal.core.backends.oci.region import make_region_clients_map
from dstack._internal.core.errors import NoCapacityError
from dstack._internal.core.models.backends.base import BackendType
from dstack._internal.core.models.instances import (
    InstanceAvailability,
    InstanceConfiguration,
    InstanceOffer,
    InstanceOfferWithAvailability,
    SSHKey,
)
from dstack._internal.core.models.runs import Job, JobProvisioningData, Requirements, Run

SUPPORTED_SHAPE_FAMILIES = [
    "VM.Standard2.",
    "VM.DenseIO1.",
    "VM.DenseIO2.",
    "VM.GPU2.",
    "VM.GPU3.",
    "VM.GPU.A10.",
]


@dataclass
class PreConfiguredResources:
    # TODO(#1194): remove this class and teach dstack to create or discover all
    # necessary resources automatically

    compartment_id: str
    subnet_ids: Dict[str, str]
    standard_image_ids: Dict[str, str]
    cuda_image_ids: Dict[str, str]

    @staticmethod
    def load(required_regions: Set[str]) -> "PreConfiguredResources":
        params = dict(
            compartment_id=os.getenv("DSTACK_OCI_COMPARTMENT_ID"),
            subnet_ids=json.loads(os.getenv("DSTACK_OCI_SUBNET_IDS", "null")),
            standard_image_ids=json.loads(os.getenv("DSTACK_OCI_STANDARD_IMAGE_IDS", "null")),
            cuda_image_ids=json.loads(os.getenv("DSTACK_OCI_CUDA_IMAGE_IDS", "null")),
        )
        for param, value in params.items():
            if not value or param.endswith("ids") and set(value) != required_regions:
                msg = (
                    f"Invalid OCI parameter {param!r}. Make sure you set the corresponding"
                    " environment variable when running dstack server"
                )
                raise ValueError(msg)
        return PreConfiguredResources(**params)


class OCICompute(Compute):
    def __init__(self, config: OCIConfig):
        self.config = config
        self.pre_conf = PreConfiguredResources.load(set(config.regions or []))
        self.regions = make_region_clients_map(config.regions or [], config.creds)

    @cached_property
    def shapes_quota(self) -> resources.ShapesQuota:
        return resources.ShapesQuota.load(self.regions, self.pre_conf.compartment_id)

    def get_offers(
        self, requirements: Optional[Requirements] = None
    ) -> List[InstanceOfferWithAvailability]:
        offers = get_catalog_offers(
            backend=BackendType.OCI,
            locations=self.config.regions,
            requirements=requirements,
            extra_filter=_supported_instances,
        )

        with ThreadPoolExecutor(max_workers=8) as executor:
            shapes_availability = resources.get_shapes_availability(
                offers, self.shapes_quota, self.regions, self.pre_conf.compartment_id, executor
            )

        offers_with_availability = []
        for offer in offers:
            if offer.instance.name in shapes_availability[offer.region]:
                availability = InstanceAvailability.AVAILABLE
            elif self.shapes_quota.is_within_region_quota(offer.instance.name, offer.region):
                availability = InstanceAvailability.NOT_AVAILABLE
            else:
                availability = InstanceAvailability.NO_QUOTA
            offers_with_availability.append(
                InstanceOfferWithAvailability(**offer.dict(), availability=availability)
            )

        return offers_with_availability

    def run_job(
        self,
        run: Run,
        job: Job,
        instance_offer: InstanceOfferWithAvailability,
        project_ssh_public_key: str,
        project_ssh_private_key: str,
    ) -> JobProvisioningData:
        instance_config = InstanceConfiguration(
            project_name=run.project_name,
            instance_name=get_instance_name(run, job),
            ssh_keys=[SSHKey(public=project_ssh_public_key.strip())],
            job_docker_config=None,
            user=run.user,
        )
        return self.create_instance(instance_offer, instance_config)

    def terminate_instance(
        self, instance_id: str, region: str, backend_data: Optional[str] = None
    ) -> None:
        region_client = self.regions[region]
        resources.terminate_instance_if_exists(region_client.compute_client, instance_id)

    def create_instance(
        self,
        instance_offer: InstanceOfferWithAvailability,
        instance_config: InstanceConfiguration,
    ) -> JobProvisioningData:
        region = self.regions[instance_offer.region]

        availability_domain = resources.choose_available_domain(
            instance_offer.instance.name, self.shapes_quota, region, self.pre_conf.compartment_id
        )
        if availability_domain is None:
            raise NoCapacityError("Shape unavailable in all availability domains")

        if len(instance_offer.instance.resources.gpus) > 0:
            image_id = self.pre_conf.cuda_image_ids[instance_offer.region]
        else:
            image_id = self.pre_conf.standard_image_ids[instance_offer.region]

        try:
            instance = resources.launch_instance(
                region=region,
                availability_domain=availability_domain,
                compartment_id=self.pre_conf.compartment_id,
                subnet_id=self.pre_conf.subnet_ids[instance_offer.region],
                display_name=instance_config.instance_name,
                cloud_init_user_data=get_user_data(instance_config.get_public_keys()),
                shape=instance_offer.instance.name,
                image_id=image_id,
            )
        except oci.exceptions.ServiceError as e:
            if e.code in ("LimitExceeded", "QuotaExceeded"):
                raise NoCapacityError(e.message)
            raise

        return JobProvisioningData(
            backend=instance_offer.backend,
            instance_type=instance_offer.instance,
            instance_id=instance.id,
            hostname=None,
            internal_ip=None,
            region=instance_offer.region,
            price=instance_offer.price,
            username="ubuntu",
            ssh_port=22,
            dockerized=True,
            ssh_proxy=None,
            backend_data=None,
        )

    def update_provisioning_data(self, provisioning_data: JobProvisioningData) -> None:
        if vnic := resources.get_instance_vnic(
            provisioning_data.instance_id,
            self.regions[provisioning_data.region],
            self.pre_conf.compartment_id,
        ):
            provisioning_data.hostname = vnic.public_ip
            provisioning_data.internal_ip = vnic.private_ip


def _supported_instances(offer: InstanceOffer) -> bool:
    if "Flex" in offer.instance.name:
        return False
    return any(map(offer.instance.name.startswith, SUPPORTED_SHAPE_FAMILIES))
